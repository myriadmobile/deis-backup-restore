FROM gliderlabs/alpine:3.1
MAINTAINER Chris Roemmich <croemmich@myriadmobile.com>

RUN apk-install \
  build-base \
  curl \
  file \
  gcc \
  git \
  libffi-dev \
  libxml2-dev \
  libxslt-dev \
  openssl-dev \
  postgresql \
  postgresql-client \
  python \
  python-dev \
  py-pip && \
  pip install virtualenv && \
  virtualenv /env

COPY app /app
WORKDIR /app

RUN /env/bin/pip install -r /app/requirements.txt

RUN apk del --purge \
      build-base \
      gcc \
      git \
      python-dev && \
    rm -rf /root/.cache \
      /usr/share/doc \
      /tmp/* \
      /var/cache/apk/*

ENTRYPOINT ["/env/bin/python", "./main.py"]