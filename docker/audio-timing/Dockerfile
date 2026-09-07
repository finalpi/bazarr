# Build the frontend first: cd frontend && npm ci --ignore-scripts && npm run build
# Then, from the repository root:
# docker build -f docker/audio-timing/Dockerfile -t finalpi/bazarr:audio-timing-v4 .
FROM lscr.io/linuxserver/bazarr@sha256:e464484e7e6cff8ee189036c9ba96977ab054104ea18067ecf6171d0f3f3f8f2
ARG REVISION=local
LABEL org.opencontainers.image.source="https://github.com/finalpi/bazarr" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.description="Bazarr with an optional offline speech-timing filter"
COPY bazarr/app/config.py /app/bazarr/bin/bazarr/app/config.py
COPY bazarr/subtitles/audio_validation.py bazarr/subtitles/download.py bazarr/subtitles/manual.py bazarr/subtitles/processing.py /app/bazarr/bin/bazarr/subtitles/
COPY bazarr/subtitles/tools/translate/services/translator_factory.py bazarr/subtitles/tools/translate/services/openai_translator.py /app/bazarr/bin/bazarr/subtitles/tools/translate/services/
COPY custom_libs/subliminal_patch/core.py custom_libs/subliminal_patch/core_persistent.py /app/bazarr/bin/custom_libs/subliminal_patch/
COPY frontend/build/ /app/bazarr/bin/frontend/build/
RUN python3 -m compileall -q /app/bazarr/bin/bazarr/subtitles /app/bazarr/bin/custom_libs/subliminal_patch /app/bazarr/bin/bazarr/app/config.py
