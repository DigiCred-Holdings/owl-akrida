# 1.6.0 ships on Python 3.13 (there is no py3.12-1.6.0 tag). If you switch
# back to py3.12-1.3.0, also revert the site-packages live-mount path in
# docker-compose.benchmark.yml from python3.13 to python3.12.
ARG ACAPY_IMAGE=ghcr.io/openwallet-foundation/acapy-agent:py3.13-1.6.0
FROM ${ACAPY_IMAGE}

# RUN pip install 'git+https://github.com/Indicio-tech/aries-acapy-cache-redis.git@154e2d01566bcea67b503e862a1843302096ed29'
#RUN pip install 'git+https://github.com/AlexanderShenshin/aries-acapy-cache-redis.git@04ba26650241eaab1d6b92e6970c5cefbb66051d'
# --no-deps: the plugin declares acapy-agent~=1.3 as a dependency, which would
# shadow the image's ACA-Py (e.g. 1.6.0) with a pip-installed 1.3.x.
RUN pip install --no-deps 'git+https://github.com/KimEbert42/aries-acapy-cache-redis.git@8be256a38d9b20d4cc571424630105b5da456032' \
    && pip install 'redis>=4.5,<6.0'
RUN pip install 'py-spy==0.4.1'

# Experimental fast-path DIDComm send plugin (local source)
COPY --chown=aries:aries ./plugins/didcomm_fastpath /tmp/didcomm_fastpath
RUN pip install /tmp/didcomm_fastpath

USER $user

#ENTRYPOINT ["/bin/bash", "-c", "aca-py \"$@\"", "--"]
ENTRYPOINT ["/bin/bash", "-c", "aca-py \"$@\"", "--"]
CMD ["start", "--arg-file", "default.yml"]
