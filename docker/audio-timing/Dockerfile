# Build the frontend first: cd frontend && npm ci --ignore-scripts && npm run build
# Then, from the repository root:
# docker build -f docker/audio-timing/Dockerfile -t ghcr.io/finalpi/bazarr:latest .
FROM lscr.io/linuxserver/bazarr@sha256:e464484e7e6cff8ee189036c9ba96977ab054104ea18067ecf6171d0f3f3f8f2
ARG REVISION=local
LABEL org.opencontainers.image.source="https://github.com/finalpi/bazarr" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.description="Bazarr with subtitle timing validation, contextual translation, and styled ASS output"
COPY bazarr/app/config.py /app/bazarr/bin/bazarr/app/config.py
COPY bazarr/subtitles/audio_validation.py bazarr/subtitles/download.py bazarr/subtitles/manual.py bazarr/subtitles/processing.py bazarr/subtitles/embedded_translation.py bazarr/subtitles/translation_priority.py /app/bazarr/bin/bazarr/subtitles/
COPY bazarr/subtitles/indexer/series.py bazarr/subtitles/indexer/movies.py /app/bazarr/bin/bazarr/subtitles/indexer/
COPY bazarr/subtitles/tools/translate/main.py bazarr/subtitles/tools/translate/traditional.py /app/bazarr/bin/bazarr/subtitles/tools/translate/
COPY bazarr/subtitles/tools/translate/services/translator_factory.py bazarr/subtitles/tools/translate/services/openai_translator.py /app/bazarr/bin/bazarr/subtitles/tools/translate/services/
COPY custom_libs/subliminal_patch/core.py custom_libs/subliminal_patch/core_persistent.py /app/bazarr/bin/custom_libs/subliminal_patch/
COPY frontend/build/ /app/bazarr/bin/frontend/build/
RUN pip install --no-cache-dir opencc-python-reimplemented==0.1.7 && \
    python3 -m compileall -q /app/bazarr/bin/bazarr/subtitles /app/bazarr/bin/custom_libs/subliminal_patch /app/bazarr/bin/bazarr/app/config.py
