# Two stages: dependencies are installed (from pinned versions) in a build stage, and the final image gets only the result,
# without pip. The base image is pinned by digest; Dependabot (.github/dependabot.yml) proposes updates for it and requirements.txt.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY requirements.txt pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN pip install --prefix=/install -r requirements.txt \
 && pip install --prefix=/install --no-deps .

FROM ${PYTHON_IMAGE}
# the release build passes the tag's version (it must match src/laminator/__init__.py) and the commit
ARG VERSION=dev
ARG REVISION=
# set only for test images built from the dev repository: the commit, shown after the version in the UI
ARG BUILD=
# TMPDIR on the data volume: large uploads (a restored backup) are spooled there, which keeps working with a read-only root filesystem
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TMPDIR=/data/.tmp LAMINATOR_BUILD=${BUILD}
COPY --from=build /install /usr/local
COPY LICENSE THIRD_PARTY_NOTICES.md /licenses/
# Hardened by default, with nothing for the user to add: no pip, and no setuid/setgid programs (su, passwd, mount, ...), so the
# unprivileged app user (no capabilities) has no way to gain privileges and cannot modify the image. --read-only, --cap-drop ALL and
# no-new-privileges remain welcome extras but are not needed for that.
RUN python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true \
 && rm -rf /root/.cache /usr/local/lib/python3*/ensurepip \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && mkdir /data && chown 99:100 /data && chmod 700 /data

# /data holds everything laminator keeps: settings, saved keys, the original posters and state.db (the only way to restore originals).
# 99:100 is the image's default unprivileged user and group (Unraid's nobody:users); run it as the owner of your data folder with `user:` (compose) or `--user`.
# Everything laminator writes is private (umask 077).
WORKDIR /data
USER 99:100
VOLUME /data

LABEL org.opencontainers.image.title="laminator" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.description="MDBList score overlays and metadata overrides for Emby/Jellyfin"

EXPOSE 5264
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD python -I -c "import urllib.request as u; u.urlopen('http://127.0.0.1:5264/healthz', timeout=4)" || exit 1

# the web UI is the default; one-off commands still work: `docker run ... laminator apply --library Movies`
ENTRYPOINT ["laminator"]
CMD ["serve", "--host", "0.0.0.0", "--port", "5264"]
