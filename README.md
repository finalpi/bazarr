<p align="center">
  <img src="frontend/public/images/logo128.png" alt="Bazarr logo" width="96">
</p>

<h1 align="center">Bazarr</h1>

<p align="center">
  A companion application to Sonarr and Radarr that manages and downloads subtitles based on your requirements.
</p>

<p align="center">
  <a href="https://github.com/morpheus65535/bazarr/issues">
    <img src="https://img.shields.io/github/issues/morpheus65535/bazarr.svg?style=flat-square" alt="GitHub issues"></a>
  <a href="https://github.com/morpheus65535/bazarr/stargazers">
    <img src="https://img.shields.io/github/stars/morpheus65535/bazarr.svg?style=flat-square" alt="GitHub stars"></a>
  <a href="https://hub.docker.com/r/linuxserver/bazarr/">
    <img src="https://img.shields.io/docker/pulls/linuxserver/bazarr.svg?style=flat-square" alt="Docker pulls - linuxserver"></a>
  <a href="https://hub.docker.com/r/hotio/bazarr/">
    <img src="https://img.shields.io/docker/pulls/hotio/bazarr.svg?style=flat-square" alt="Docker pulls - hotio"></a>
  <a href="https://github.com/finalpi/bazarr/actions/workflows/docker-publish.yml">
    <img src="https://github.com/finalpi/bazarr/actions/workflows/docker-publish.yml/badge.svg?branch=master" alt="Enhanced Docker image build"></a>
  <a href="https://discord.gg/MH2e2eb">
    <img src="https://img.shields.io/badge/discord-chat-MH2e2eb.svg?style=flat-square" alt="Discord"></a>
</p>

## About

Bazarr is a companion application to Sonarr and Radarr. It manages and downloads subtitles based on your requirements. You define your preferences by TV show or movie, and Bazarr takes care of everything for you.

Bazarr does not scan your disk to detect series and movies. It only manages the series and movies that are indexed in Sonarr and Radarr.

## Enhanced subtitle workflow in this fork

The `master` branch and its Docker image extend Bazarr with a subtitle workflow designed for mixed-language TV and anime libraries:

- Validate downloaded subtitles against speech timing before saving them. A failed subtitle is aligned first and then validated again; it is rejected when the aligned result still does not match.
- Prefer a matching embedded subtitle track as the synchronization reference, with audio timing available as the fallback.
- Extract embedded text subtitle tracks for translation. The work's original-language track is preferred, followed by English.
- Download the original-language subtitle when no usable embedded or external source exists. Sonarr/Radarr `originalLanguage` metadata is used, so Japanese works request and retain `ja` subtitles.
- Translate with Ollama or any OpenAI-compatible API using neighboring cues for context, balanced line breaking, bilingual output, and low-priority `.llm.<language>` filenames.
- Save three additional OpenAI-compatible API profiles and switch the active endpoint, API key, and model together from **Settings > Subtitles > Translating**. The original fields remain available as the Default profile.
- Label external subtitles without a language suffix as Unknown instead of guessing from mixed-language content. Automatic LLM translation ignores Unknown files and prefers an embedded original-language or English subtitle track. Legacy subtitle encodings such as GB18030 are detected when a marked source is translated manually.
- Allow manual translation directly from a displayed embedded text subtitle track. Bazarr extracts the selected stream to its cache before translation; other external-subtitle tools remain disabled for embedded tracks.
- Label subtitle badges by source where needed: `MISSING` for unmet language requirements, `LLM` for generated fallback subtitles, and `EMBEDDED` for video subtitle tracks.
- Format Simplified Chinese dialogue using a streaming-style punctuation policy: commas and periods become single spaces while meaningful question marks, exclamation marks, colons, quotes, and ellipses remain. Two speakers in one cue keep separate ASCII `-` markers on the same line. Translation prompts target concise lines up to 18 Chinese characters, with a hard display limit of 24. The default 1080p bilingual style uses 64px Chinese, 48px original text, and a 20px bottom margin, based on a comfortable YYeTs bilingual ASS reference.
- Translate Simplified Chinese once and create Traditional Chinese locally with OpenCC, avoiding a second LLM request.
- Save LLM subtitles as styled ASS files with separate `Chinese` and `Original` styles. The fansub-inspired defaults put larger bold yellow Chinese above smaller white original text; both styles and the bottom margin can be adjusted independently with a live preview under **Settings > Subtitles > LLM Subtitle Appearance**.
- Send configured Apprise notifications, including Telegram, when an LLM translation completes or fails. Success messages include the target language, model, and output filename; failure messages include a short redacted reason.
- Notify Jellyfin after subtitle downloads, uploads, deletions, synchronization, and translations. Configure the Jellyfin server, API key, libraries, and immediate or asynchronous refresh under **Settings > Jellyfin**.

Provider subtitles remain authoritative. LLM subtitles are retained as a fallback and do not stop Bazarr from searching for a matching provider subtitle. Embedded Chinese prevents an unnecessary LLM translation, but it does not count as an external downloaded Chinese subtitle.

Detailed behavior and benchmark notes are available in [audio timing filter](docs/audio-timing-filter.md), [audio timing benchmark](docs/audio-timing-benchmark.md), and [OpenAI-compatible translation](docs/openai-compatible-translation.md).

## Enhanced Docker image

Every push to `master` publishes a multi-platform image for `linux/amd64` and `linux/arm64` to GitHub Container Registry:

```bash
docker pull ghcr.io/finalpi/bazarr:latest
```

Use it in an existing LinuxServer Bazarr Compose file by changing only the image:

```yaml
services:
  bazarr:
    image: ghcr.io/finalpi/bazarr:latest
    container_name: bazarr
    volumes:
      - ./config:/config
      - /path/to/tv:/tv
      - /path/to/movies:/movies
    ports:
      - "6767:6767"
    restart: unless-stopped
```

Available tags are `latest`, `master`, and `sha-<commit>`. Keep the existing `/config`, `/tv`, and `/movies` mappings when replacing another LinuxServer Bazarr image. The first workflow run creates the GHCR package; its visibility must be public for anonymous pulls.

## Links

| Resource         | Link                                                            |
| ---------------- | --------------------------------------------------------------- |
| Documentation    | [Wiki](https://wiki.bazarr.media)                               |
| Support          | [Discord](https://discord.gg/MH2e2eb)                           |
| Bug reports      | [GitHub Issues](https://github.com/morpheus65535/bazarr/issues) |
| Feature requests | [Feature Upvote](http://features.bazarr.media)                  |

## Support the project

At the request of some users, here is a way to show appreciation for the efforts made in the development of Bazarr:

[![Donate](https://img.shields.io/badge/Donate-PayPal-green.svg)](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=XHHRWXT9YB7WE&source=url)

## Major features

- Support for major platforms: Windows, Linux, macOS, Raspberry Pi, etc.
- Automatically add new series and episodes from Sonarr.
- Automatically add new movies from Radarr.
- Series- and movie-based subtitle language configuration.
- Scan your existing library for internal and external subtitles and download any missing ones.
- Keep a history of what was downloaded, from where, and when.
- Manual search to download subtitles on demand.
- Upgrade previously downloaded subtitles when a better one is found.
- Delete external subtitles from disk.
- Support for 184 subtitle languages, including forced/foreign subtitles depending on providers.
- A beautiful UI based on Sonarr.

## Supported subtitle providers

- Addic7ed
- AnimeKalesi
- Animetosho (requires AniDb HTTP API client described [here](https://wiki.anidb.net/HTTP_API_Definition))
- AnimeSub.info
- Assrt
- AvistaZ, CinemaZ (Get session cookies using method described [here](https://github.com/morpheus65535/bazarr/pull/2375#issuecomment-2057010996))
- BetaSeries
- BSplayer
- Embedded Subtitles
- Gestdown.info
- GreekSubs
- GreekSubtitles
- HDBits.org
- Hosszupuska
- Karagarga.in
- Ktuvit (Get `hashed_password` using method described [here](https://github.com/XBMCil/service.subtitles.ktuvit))
- LegendasDivx
- Legendas.net
- Napiprojekt
- Napisy24
- Nekur
- OpenSubtitles.com
- OpenSubtitles.org (VIP users only)
- Pipocas.tv
- Prijevodi-Online
- RegieLive
- Sous-Titres.eu
- SubDL
- subf2m.co
- Subs.sab.bz
- Subs4Free
- Subs4Series
- Subsarr (self-hosted, requires [slimcdk/subsarr](https://github.com/slimcdk/subsarr))
- Subscene
- Subscenter
- SubsRo
- Subsunacs.net
- SubSynchro
- Subtis
- Subtitrari-noi.ro
- subtitri.id.lv
- Subtitulamos.tv
- SubX
- Supersubtitles
- Titlovi
- Titrari.ro
- Titulky.com
- Turkcealtyazi.org
- TuSubtitulo
- TVSubtitles
- Whisper (requires [ahmetoner/whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice))
- Wizdom
- XSubs
- Yavka.net
- YIFY Subtitles
- Zimuku

## Screenshot

![Bazarr](screenshot/bazarr-screenshot.png?raw=true "Bazarr")

## Acknowledgements

Thanks to the folks at OpenSubtitles for their logo, which inspired ours.

## License

- [GNU GPL v3](http://www.gnu.org/licenses/gpl.html)
- Copyright 2010-2026
