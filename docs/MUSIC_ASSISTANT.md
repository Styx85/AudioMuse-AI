# Music Assistant

AudioMuse-AI can analyze the library of a [Music Assistant](https://music-assistant.io)
server (2.10 or newer) directly, without Jellyfin, Navidrome or Plex in between.

## How it works

- The track list, albums, playlists and play counts come from Music Assistant's JSON API
  (`POST <url>/api` with `{"command": ..., "args": ...}` and a Bearer token).
- Track and album ids are Music Assistant **library ids**. Clients such as the
  [Music Assistant AudioMuse-AI plugin](https://github.com/Styx85/music-assistant-audiomuse)
  can therefore use AudioMuse-AI results directly (similar tracks, radio mode, autoplay).
- Audio is read from a **shared music folder**: Music Assistant has (by design) no download
  endpoint. Mount the folder that Music Assistant's *filesystem* provider uses into the
  AudioMuse-AI flask and worker containers; read-only is enough. AudioMuse-AI appends the
  relative path Music Assistant stores for each track.
- Only tracks of Music Assistant's local filesystem providers (`filesystem_local`,
  `filesystem_smb`, `filesystem_nfs`, ...) are analyzed; streaming-service tracks are skipped.
- "Music libraries" are Music Assistant's filesystem provider instances, so the library
  filter selects providers by instance id or name.
- Playlists created by AudioMuse-AI appear as Music Assistant playlists.

## Configuration

| Setup field | Environment variable | Example |
| --- | --- | --- |
| Music Assistant URL | `MUSICASSISTANT_URL` | `http://music-assistant:8095` |
| Access token | `MUSICASSISTANT_TOKEN` | a Music Assistant access token |
| Music folder (in AudioMuse-AI) | `MUSICASSISTANT_MUSIC_PATH` | `/music` |

For several filesystem providers with different roots use
`provider_instance=/path;other_instance=/path2` as music folder.

The token needs read access to providers and the library and write access to playlists.
With system read access AudioMuse-AI also checks that tracks were really added to a
playlist (Music Assistant adds them in a background task); without it, it assumes success.

Docker Compose excerpt:

```yaml
  audiomuse-ai-worker:
    volumes:
      - /srv/music:/music:ro   # the same folder Music Assistant's filesystem provider reads
```

**Test connection** warns when the music folder is not set or no sample file is readable.
