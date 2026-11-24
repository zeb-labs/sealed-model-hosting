# 02. Technical design

One flow of 19 steps in four phases. For each step: what we configure, how it
works, why. Steps marked (new) do not exist in the AWS sample. Nothing here has
been run yet.

Lanes: B = builder account, S = sealed machine, C = customer account.

## Phase A: build (builder side, once per model version)

### 1. Encrypt weights (B)
- Configure: a symmetric KMS key; an S3 bucket the customer's instance role may
  read.
- How: `kms:GenerateDataKey` returns a data key in plain and wrapped form. The
  plain form encrypts the weights with tensorizer (2 MiB chunks, each
  authenticated, XSalsa20-Poly1305), then is discarded. The wrapped form is
  stored next to the weights.
- Why: S3 holds only ciphertext. Chunks allow streaming and detect tampering.
  The sample uses AES-CBC with no tamper check.

### 2. Build containers (B)
- Configure: three containers (vLLM server, loader, trainer) and a manifest
  listing the SHA-256 of each archive as stored in S3.
- Why: the sample loads whatever is in the bucket. Digests fix which code runs.

### 3. Build sealed image (B)
- Configure: KIWI recipe on Amazon Linux 2023. UKI, erofs read-only root,
  dm-verity, Secure Boot keys. Removed: sshd, SSM agent, cloud-init, EC2
  Instance Connect, serial login. Baked in: the manifest, bucket name, key ARN.
- How: registered as an AMI with UEFI boot and NitroTPM 2.0, then shared. The
  dm-verity root hash is in the kernel command line inside the UKI.
- Why: everything that decides what runs is inside the measured image.

### 4. Compute PCR values (B)
- Configure: `nitro-tpm-pcr-compute` on the UKI; a script that computes the
  expected runtime PCR from the manifest.
- Output: PCR4 (boot file), PCR7 (Secure Boot state), PCR12 (command-line
  overrides and add-on boot files), runtime PCR (hash chain of the manifest).

### 5. Write key policy (B)
- Configure: one KMS key per customer, each wrapping the same data key.
  `kms:Decrypt` allowed for that customer's instance role only when every
  pinned PCR matches. No general IAM delegation for Decrypt.
- How: the pipeline writes the policy from step 4. During rollout it lists the
  current and previous image.
- Why: the key is tied to the image, not to who asks. KMS grants cannot carry
  attestation conditions, so we use one key per customer, not grants.

```json
{
  "Sid": "ReleaseToSealedImageOnly",
  "Effect": "Allow",
  "Principal": {"AWS": "arn:aws:iam::CUSTOMER_ACCOUNT:role/SEALED_INSTANCE_ROLE"},
  "Action": "kms:Decrypt",
  "Resource": "*",
  "Condition": {
    "StringEqualsIgnoreCase": {
      "kms:RecipientAttestation:NitroTPMPCR4": "PCR4_VALUE",
      "kms:RecipientAttestation:NitroTPMPCR7": "PCR7_VALUE",
      "kms:RecipientAttestation:NitroTPMPCR12": "PCR12_VALUE",
      "kms:RecipientAttestation:NitroTPMPCR15": "RUNTIME_OPEN_VALUE"
    }
  }
}
```

PCR4 and PCR7 lines come from the sample. PCR12 and the runtime line (PCR15 is
a placeholder index) follow the same pattern and must be confirmed against KMS.

## Phase B: install and launch (customer side)

### 6. Install stack (C)
- Configure: one CloudFormation stack. Private subnet with no internet route.
  S3 gateway endpoint; interface endpoints for KMS, STS, CloudWatch Logs.
  Security group: inbound only on the API port from the customer's app; no SSH.
  Instance role: read builder bucket, `kms:Decrypt` on the builder key, write
  logs in the customer's own account, use the customer's own KMS key. A
  customer-owned KMS key and bucket for adapters.
- Why: the endpoint list is the complete list of places the machine can reach.

### 7. Launch instance (C)
- Configure: launch template with the shared AMI, a GPU instance type, IMDSv2
  required with hop limit 1, the instance role, no key pair.
- Why: no launch setting can open a door. Hop limit 1 keeps containers away
  from the instance credentials.

## Phase C: boot, unlock and load (every start)

### 8. Measured boot (S)
- How: UEFI firmware hashes each boot component into the NitroTPM before
  running it. UKI -> PCR4, Secure Boot state -> PCR7, command-line overrides and
  add-on boot files -> PCR12. dm-verity checks every disk block.
- Why: a PCR can only be extended, never set.

### 9. Check containers (S, new)
- Configure: the loader, a small host service and the only process allowed to
  use `/dev/tpm0`.
- How: downloads the container archives, compares SHA-256 with the manifest,
  then extends the runtime PCR with the manifest hash (`tpm2_pcrextend`). Stops
  on a mismatch.
- Why: ties post-boot code to the proof and puts the machine in the "open"
  state KMS expects.

### 10. Attest to KMS (S)
- How: the loader makes a throwaway RSA key pair in memory, gets an attestation
  document that includes the public key (`nitro-tpm-attest`), and calls
  `kms:Decrypt` with that document as `Recipient`
  (`KeyEncryptionAlgorithm: RSAES_OAEP_SHA_256`).

### 11. KMS releases key (B)
- How: KMS verifies the document's signature chain, compares PCRs with the
  policy, and returns the data key encrypted to the throwaway public key
  (`CiphertextForRecipient`). The loader unwraps it in memory. CloudTrail
  records the call.

### 12. Lock the door (S, new)
- How: right after unwrapping, the loader extends the runtime PCR again with a
  fixed "locked" value and deletes the throwaway private key. The PCR no longer
  matches the model-key policy.
- Why: code that lands later cannot fetch the key.

### 13. Stream to GPU (S, new)
- Configure: vLLM `--load-format tensorizer` with `tensorizer_uri` and
  `encryption_keyfile` in `--model-loader-extra-config`. The key sits in a
  RAM-only file readable only by the vLLM user.
- How: the loader gives vLLM a pre-signed link to the ciphertext. vLLM's
  tensorizer loader fetches chunks in parallel, checks and decrypts each, and
  writes it to the GPU. The key file is removed after loading.
- Why: host RAM stays flat, vLLM is unmodified, and the vLLM container needs no
  AWS credentials and no TPM.
- Second path: local NVMe instance store under dm-crypt with a random key that
  lives only in RAM. Used for training checkpoints, frequent restarts, and
  runtimes other than vLLM.

### The runtime PCR has two states

| State | Value | When | Model key | Lease key |
| --- | --- | --- | --- | --- |
| Open | Extended once with the manifest hash | Between step 9 and 12 | Released | Refused |
| Serving | Extended again with "locked" | From step 12 on | Refused | Released |

The lease key is a second KMS key per customer whose policy pins the Serving
value. It is used only for renewals in step 18.

## Phase D: run (continuously)

### 14. Serve with vLLM (S)
- Configure: container read-only root, non-root user, all capabilities dropped,
  no shell endpoints, no TPM. Per-token probabilities off by default. LoRA
  adapters enabled.

### 15. Send requests (C)
- The customer's app calls the machine's private address inside their VPC.

### 16. Fine-tune (S, new)
- Configure: a JSON job form with a fixed schema: data location in the
  customer's S3, adapter method, bounded adapter rank, steps, learning rate.
  The trainer container is in the manifest.
- How: the trainer validates the form, reads the data, trains a LoRA adapter
  against the base weights. Checkpoints go only to the encrypted local disk.
- Why: the customer supplies data and settings, never code.

### 17. Adapter and logs (C, new)
- The trainer encrypts the adapter under the customer's own KMS key and stores
  it in the customer's bucket. vLLM loads it. All system and container logs
  stay in the customer's CloudWatch.

### 18. Usage and lease (S, new)
- Configure: a fixed-schema usage record (instance, model version, time window,
  request count, token counts); a lease interval.
- How: a meter writes the record to the builder's log group through a narrow
  cross-account role. At each interval the loader attests again and asks KMS to
  decrypt a small lease token under the lease key. On failure, serving stops
  and GPU memory is cleared.

### 19. Allow or cut off (B, new)
- To cut off: disable that customer's model key and lease key. The next start
  and the next renewal both fail.
- To update: add the new image's PCR values, wait for customers to move, remove
  the old ones.

## The deployer and the attack suite

- AgentCore deployer runs steps 1 to 6 by calling fixed pipeline tools. It must
  never see plain weights or keys, never write key policy text itself, and
  never skip the human approval before step 5.
- The assurance suite attacks each build and produces an evidence pack.

| Test | Expected | Property |
| --- | --- | --- |
| Scan for SSH, SSM, serial login, web terminals | None | P2 |
| Change bucket tag, DNS, launch settings | No effect | P1 |
| Edit boot partition or kernel command line | KMS refuses | P1 |
| Swap a container archive | Loader stops before asking for the key | P1 |
| Ask KMS for the model key after step 12 | Refused | P1 |
| Search disk and snapshots for readable weights | None | P3 |
| Capture outbound traffic | Only listed endpoints; only usage records to builder | P4 |
| Training job with extra fields or code | Rejected | P5 |
| Disable the customer's keys | Serving stops within one lease interval | P6 |

## Must verify before relying on the design

1. KMS accepts a condition on PCR12 and on a runtime PCR index.
2. A custom AMI can enforce Secure Boot with our own keys and a signed UKI.
3. Extending a PCR from inside the instance shows up in the attestation
   document.
4. The dm-verity root hash really is inside the UKI on our build.
5. The GPU instance types we use support NitroTPM attestation.
6. vLLM tensorizer loading works with encryption on more than one GPU.
7. A pre-signed S3 link works as `tensorizer_uri`.
8. Which vLLM setting switches off per-token probabilities.
9. Training and serving can share one GPU, or serving pauses during training.
10. Lease renewal works with the Serving-state PCR and a second key.

## Where the facts come from

- `Recipient` exchange, `nitro-tpm-attest`, PCR4 and PCR7 condition keys: the
  AWS sample code.
- `--load-format tensorizer`, `encryption_keyfile`, direct-to-GPU loading,
  `--cpu-offload-gb`: vLLM source (`vllm/model_executor/model_loader/tensorizer.py`,
  `vllm/config/offload.py`).
- 2 MiB authenticated chunks: tensorizer `docs/encryption.md`.
- PCR12 meaning: systemd-stub manual.
- AWS docs: EC2 instance attestation, Attestable AMIs, compute PCR
  measurements, KMS condition keys for NitroTPM.
