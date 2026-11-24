# What is original, and what we changed

The starting point is the AWS sample
`aws-samples/sample-guarded-user-controlled-attested-runtime-deployment`
(commit `b82acf76e4650f26e87e063fad1d643ca1101c57`). This page says what we
kept from it, what we changed, and what we added.

Each line is marked: **Run** (we executed it and have the output), or
**Read in code** (we saw the exact lines; for the sample this means we read
it and did not run it).

## Kept from the original: the core idea

The sample's central mechanism is sound, and we kept it unchanged:

1. The builder encrypts the model. The key stays in AWS KMS.
2. The builder ships a sealed machine image (Amazon Linux 2023, built with KIWI, a signed boot file, a read-only verified disk).
3. At boot the machine sends KMS a NitroTPM attestation document, signed by AWS hardware, that states what it booted.
4. KMS releases the key only if those measurements match the key policy, wrapped so only that one boot can open it.

Everything below is around that core.

## Changed: gaps we found in the sample, and what we did

| # | In the original (Read in code) | In ours | Status |
| --- | --- | --- | --- |
| 1 | A remote-login agent (Systems Manager) is installed in the image, and the machine's role allows it | No login path of any kind: no SSH, no Systems Manager agent, no serial or rescue login. The build refuses an image that has one. Even with the role and network path added, the machine never registers. | Run |
| 2 | A web terminal with no login runs next to the model, on an open port, in a container that can reach the TPM | Removed. One open port (HTTPS 443) that passes four fixed paths to the model server and needs the customer's API key | Run |
| 3 | The customer chooses where code is loaded from (a launch tag); containers are not checked | Every container and file is compared with fingerprints inside the measured image before any key is requested. A launch tag may choose a location, never what is accepted. | Run |
| 4 | A remote "run any command" channel from a control server into the machine and into the model's container, over a plain connection the customer can imitate | Removed. Nothing can send the machine a command. The builder's only control is a key in KMS. | Run |
| 5 | The key policy pins two measurements (PCR4, PCR7); the image is registered without Secure Boot key data | Four measurements (PCR4, 7, 12, and our own PCR15), with Secure Boot and our own keys. A changed boot file, changed disk, missing Secure Boot data or wrong role each end with no key. | Run |
| 6 | The machine can ask KMS for the key again at any time, and every container can reach the TPM | One request per boot. The loader then locks the machine's state so the key policy no longer matches. Only the loader can reach the TPM. | Run |
| 7 | Encryption with no tamper check (AES-CBC); the key is printed to a log | Authenticated encryption in 8 MiB chunks (AES-256-GCM); a changed, reordered or cut file is refused. Keys never reach a log. | Run |
| 8 | All machine and model logs, including full model answers, go to the builder's account | Logs go to the customer's own log group. The builder gets sealed usage counts only, in a folder the customer can read first. | Run |
| 9 | The whole model is held in RAM in plain form, so model size is capped by machine memory | The model streams from encrypted storage to the GPU through a local proxy; vLLM runs unmodified. Plain weights never touch the system disk. | Run |
| 10 | No fine-tuning | The customer sends data and a form, never code. Training runs with no network; only adapter-shaped files leave, under the customer's key. | Run |
| 11 | No way to stop a running machine; one key for everyone | A lease the machine renews every minute. Disable the key and it stops within about two minutes. One customer can be cut off alone. | Run |
| 12 | Measurements pasted into the key policy by hand on every rebuild | The pipeline computes them and writes both key policies, after an approval tied to that exact build | Run |
| 13 | Setup is 11 manual steps across two accounts | Two commands: `bash smh.sh ship` (builder) and `bash smh.sh up` (customer), each watched by an AI agent that fixes or explains failures | Run |
| 14 | Nothing tests the finished image | A suite of 33 tests, including 8 attacks, judged from raw evidence. 32 pass on release 0.10.0. | Run |
| 15 | The customer's machine role has broad read rights and full S3 access | A role with only what the machine needs; no KMS right of its own | Run |
| 16 | The model's container can open connections out | A firewall set at boot: the container reaches the local proxy and nothing else | Run |

Two points in the original walkthrough did not match the code at that commit:
it says the Systems Manager agent is left out (it is installed), and it does
not mention the web terminal.

## Added: things the original did not have

| Addition | What it does | Status |
| --- | --- | --- |
| One image for many customers | The customer's bucket, key and log group come from launch tags, checked for shape. They open nothing. | Run |
| Per-customer access | One statement per customer on both keys, with the same measurements. Removing it cuts that customer off. | Run |
| Customer-side model copy | The encrypted model can sit in the customer's own bucket; it is unreadable without the builder's key. | Run |
| Stable endpoint | An internal load balancer gives the endpoint a name that survives restarts. | Run |
| Release agent | An AI agent (AWS AgentCore) runs the builder's release steps through fixed tools. It cannot write a key rule or read a secret. | Run |
| Watcher agent | An AI agent that is called when any step fails, on either side. It investigates with read-only tools, applies a fix from a short fixed list, or tells the person exactly what to do, then the step runs again. | Run: a customer with no key access, a gated model, no free GPU machine (fixed by itself), a fine-tune over the limit. A missing AWS permission was tried on a made-up error only. |
| Any open model, or the builder's own weights | `ship` takes a Hugging Face model by name, or a folder with the builder's own weights (disk, network share or S3). The own weights are staged, encrypted, and the staged copy is deleted. | Run: an open model by name (Qwen2.5-1.5B) and the builder's own weights from a folder (a Llama-architecture model, SmolLM2-360M, standing in for a self-trained one) were each released, served and fine-tuned. |
| Cost guard | Tracks spend, stops the project's machines nightly and near the budget. | Run |

## Not done, said plainly

- **One AWS account.** Builder and customers are separate sets of resources in one account. The real wall needs a second account. Not tried.
- **Two small models so far**, of two architectures (Qwen2 and Llama), one by name and one from a folder. Both are small and run on one GPU; a large model, a multi-GPU model and other architectures are untested. Gated or private Hugging Face models cannot be fetched by name; ship their weights from a folder instead. An own model from an `s3://` location uses the same path as a folder but was not run.
- **The build is not reproducible yet**, so a customer cannot rebuild the image and compare.
- **Secure Boot uses test keys.** The endpoint certificate is self-signed per boot.
- **Approval is automatic** when six checks pass. A person reading each release before approving is not shown.
- **The customer template creating its own network** was not run (the test account is at its limit of five networks); it was run inside an existing network.
