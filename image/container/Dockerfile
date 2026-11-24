FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
RUN dnf install -y python3 && dnf clean all
COPY server.py /srv/server.py
USER 65534:65534
EXPOSE 8000
CMD ["python3", "/srv/server.py"]
