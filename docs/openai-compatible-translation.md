# OpenAI-Compatible Subtitle Translation

Bazarr can translate subtitles through an OpenAI-compatible chat-completions API.
This supports a local Ollama server and OpenAI's GPT API with the same translator.

## Configuration

Select **OpenAI Compatible (Ollama / GPT)** under Settings > Subtitles >
Translating. For Ollama running on the Docker host, use:

```text
Base URL: http://host.docker.internal:11434/v1
Model: translategemma:12b
API key: (empty)
```

For OpenAI, use `https://api.openai.com/v1`, an API model name, and an OpenAI
API key. A ChatGPT subscription does not itself provide API access.

Each request translates a target batch and includes configurable neighboring
cues as read-only context. The response must contain every requested numeric cue
index exactly once. Missing, extra, duplicate or empty translations cause a retry;
after three failures no partial output is installed. Output is written to a
temporary file and atomically moved into place only after the whole subtitle is
translated.

The translator preserves cue timestamps and uses deterministic punctuation-aware
line wrapping. Bilingual mode writes the original English followed by Chinese in
each cue. It does not ask the model to invent or redistribute timestamps.

## Automatic Mode

Enable **Automatically translate English when Chinese subtitles are missing**.
After an English subtitle has been downloaded and processed, Bazarr queues a
translation only if all of these conditions hold:

- the source subtitle is English and is not forced;
- neither simplified nor traditional Chinese is indexed as an external or
  embedded subtitle;
- the expected Chinese output file does not already exist.

Automatic mode uses the currently selected translator. With the OpenAI-compatible
translator it produces the same context-aware, optionally bilingual output as a
manual translation. Existing libraries are not swept when the switch is enabled;
the trigger applies when a new English download completes.
