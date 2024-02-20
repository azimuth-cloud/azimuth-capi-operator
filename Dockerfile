FROM ubuntu:jammy as helm

RUN apt-get update && \
    apt-get install -y curl && \
    rm -rf /var/lib/apt/lists/*

ARG HELM_VERSION=v3.14.1
RUN set -ex; \
    OS_ARCH="$(uname -m)"; \
    case "$OS_ARCH" in \
        x86_64) helm_arch=amd64 ;; \
        aarch64) helm_arch=arm64 ;; \
        *) false ;; \
    esac; \
    curl -fsSL https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz | \
      tar -xz --strip-components 1 -C /usr/bin linux-${helm_arch}/helm; \
    helm version

# Pull and unpack the baked in charts
ARG OPENSTACK_CLUSTER_CHART_REPO=https://stackhpc.github.io/capi-helm-charts
ARG OPENSTACK_CLUSTER_CHART_NAME=openstack-cluster
ARG OPENSTACK_CLUSTER_CHART_VERSION=0.4.0
RUN helm pull ${OPENSTACK_CLUSTER_CHART_NAME} \
      --repo ${OPENSTACK_CLUSTER_CHART_REPO} \
      --version ${OPENSTACK_CLUSTER_CHART_VERSION} \
      --untar \
      --untardir /charts && \
    rm -rf /charts/*.tgz

ARG ZENITH_CHART_REPO=https://stackhpc.github.io/zenith
ARG ZENITH_CHART_VERSION=0.5.0
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


FROM ubuntu:jammy AS python-builder

RUN apt-get update && \
    apt-get install -y python3 python3-venv && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /venv && \
    /venv/bin/pip install -U pip setuptools

COPY requirements.txt /app/requirements.txt
RUN /venv/bin/pip install --no-deps --requirement /app/requirements.txt

# Jinja2 complains if this is installed the "regular" way
# https://jinja.palletsprojects.com/en/3.1.x/api/#loaders
# So we install here instead as an editable installation and also copy over the app directory
COPY . /app
RUN /venv/bin/pip install --no-deps -e /app


FROM ubuntu:jammy

# Don't buffer stdout and stderr as it breaks realtime logging
ENV PYTHONUNBUFFERED 1

# Tell Helm to use /tmp for mutable data
ENV HELM_CACHE_HOME /tmp/helm/cache
ENV HELM_CONFIG_HOME /tmp/helm/config
ENV HELM_DATA_HOME /tmp/helm/data

# Create the user that will be used to run the app
ENV APP_UID 1001
ENV APP_GID 1001
ENV APP_USER app
ENV APP_GROUP app
RUN groupadd --gid $APP_GID $APP_GROUP && \
    useradd \
      --no-create-home \
      --no-user-group \
      --gid $APP_GID \
      --shell /sbin/nologin \
      --uid $APP_UID \
      $APP_USER

RUN apt-get update && \
    apt-get install --no-install-recommends --no-install-suggests -y ca-certificates python3 tini && \
    rm -rf /var/lib/apt/lists/*

COPY --from=helm /usr/bin/helm /usr/bin/helm
COPY --from=helm /charts /charts
COPY --from=python-builder /venv /venv
COPY --from=python-builder /app /app

USER $APP_UID
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD [ \
    "/venv/bin/kopf", \
    "run", \
    "--module", \
    "azimuth_capi.operator", \
    "--all-namespaces", \
    "--liveness", \
    "http://0.0.0.0:8000/healthz" \
]
