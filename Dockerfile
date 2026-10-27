# ubuntu 24.04
FROM ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS helm

RUN apt-get update && \
    apt-get install --no-install-recommends --no-install-suggests -y ca-certificates wget && \
    rm -rf /var/lib/apt/lists/*

ARG HELM_VERSION=v4.3.1
RUN set -ex; \
    OS_ARCH="$(uname -m)"; \
    case "$OS_ARCH" in \
        x86_64) helm_arch=amd64 ;; \
        aarch64) helm_arch=arm64 ;; \
        *) false ;; \
    esac; \
    wget -q -O - https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz | \
      tar -xz --strip-components 1 -C /usr/bin linux-${helm_arch}/helm; \
    helm version

# Pull and unpack the baked in charts
ARG OPENSTACK_CLUSTER_CHART_REPO=https://azimuth-cloud.github.io/capi-helm-charts
ARG OPENSTACK_CLUSTER_CHART_NAME=openstack-cluster
ARG OPENSTACK_CLUSTER_CHART_VERSION=0.29.3
RUN helm pull ${OPENSTACK_CLUSTER_CHART_NAME} \
      --repo ${OPENSTACK_CLUSTER_CHART_REPO} \
      --version ${OPENSTACK_CLUSTER_CHART_VERSION} \
      --untar \
      --untardir /charts && \
    rm -rf /charts/*.tgz

ARG ZENITH_CHART_REPO=https://azimuth-cloud.github.io/zenith
ARG ZENITH_CHART_VERSION=0.21.1
ARG ZENITH_APISERVER_CHART_NAME=zenith-apiserver
ARG ZENITH_OPERATOR_CHART_NAME=zenith-operator
RUN helm pull ${ZENITH_APISERVER_CHART_NAME} \
      --repo ${ZENITH_CHART_REPO} \
      --version ${ZENITH_CHART_VERSION} \
      --untar \
      --untardir /charts && \
    helm pull ${ZENITH_OPERATOR_CHART_NAME} \
      --repo ${ZENITH_CHART_REPO} \
      --version ${ZENITH_CHART_VERSION} \
      --untar \
      --untardir /charts && \
    rm -rf /charts/*.tgz

FROM ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS python-builder

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

# These env vars setup UV for a docker installation as opposed to
# the defaults which are optimised for local development.
# https://docs.astral.sh/uv/guides/integration/docker/#optimizations
# https://docs.astral.sh/uv/reference/environment/
ENV UV_NO_DEV=1 \
    UV_NO_EDITABLE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_CACHE_DIR=/uv-cache/ \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_INSTALL_BIN=0 \
    UV_PYTHON_PREFERENCE=only-managed

WORKDIR /app-source

### INSTALL PINNED PYTHON ###
# https://docs.astral.sh/uv/guides/install-python/
COPY .python-version /app-source
RUN uv python install \
    && chmod -R ugo=rX /python

### INSTALL PROJECT INTO VENV ###
# uv sync --active makes uv (re)create the currently
# active venv and install into it.
ENV VIRTUAL_ENV=/app

### INSTALL PINNED DEPENDENCIES ###
# --frozen makes uv install the versions which are pinned in uv.lock
# Installing the dependencies first optimizes caching so if the app
# changes but not the deps there is no need to rebuild this.
COPY uv.lock pyproject.toml /app-source
RUN --mount=type=cache,target=/uv-cache/ \
    uv sync --active \
            --frozen \
            --no-install-project \
    && chmod -R ugo=rX /app

### INSTALL THE PROJECT ###
# Then install the app.
COPY . /app-source
RUN --mount=type=cache,target=/uv-cache/ \
    uv sync --active \
            --frozen \
    && chmod -R ugo=rX /app


FROM ubuntu@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS final 

# Don't buffer stdout and stderr as it breaks realtime logging
ENV PYTHONUNBUFFERED=1

# Make httpx use the system trust roots
# By default, this means we use the CAs from the ca-certificates package
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Tell Helm to use /tmp for mutable data
ENV HELM_CACHE_HOME=/tmp/helm/cache
ENV HELM_CONFIG_HOME=/tmp/helm/config
ENV HELM_DATA_HOME=/tmp/helm/data

# Create the user that will be used to run the app
ENV APP_UID=1001
ENV APP_GID=1001
ENV APP_USER=app
ENV APP_GROUP=app
RUN groupadd --gid $APP_GID $APP_GROUP && \
    useradd \
      --no-create-home \
      --no-user-group \
      --gid $APP_GID \
      --shell /sbin/nologin \
      --uid $APP_UID \
      $APP_USER

RUN apt-get update && \
    apt-get install --no-install-recommends --no-install-suggests -y ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=helm /usr/bin/helm /usr/bin/helm
COPY --from=helm /charts /charts
COPY --from=python-builder /python /python
COPY --from=python-builder /app /app

USER $APP_UID
CMD ["/app/bin/python", "-m", "azimuth_capi_operator"]
