# AudioMuse-AI - https://github.com/NeptuneHub/AudioMuse-AI
# Copyright (C) 2025 NeptuneHub
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root or <https://github.com/NeptuneHub/AudioMuse-AI/blob/main/LICENSE>

"""Music Assistant backend for the AudioMuse-AI media-server abstraction.

Implements the provider interface against Music Assistant's JSON API
(``POST <url>/api`` with ``{"command": ..., "args": ...}`` and a Bearer token,
see ``<url>/api-docs``). Dispatched by tasks/mediaserver/__init__.py when the
active server type is 'musicassistant'.

Main Features:
* Track and album ids are Music Assistant LIBRARY ids, so the Music Assistant
  AudioMuse plugin can use AudioMuse-AI results without any matching.
* Only tracks of Music Assistant's local filesystem providers are offered: their
  audio is read straight from a shared music folder (``music_path``), because
  Music Assistant deliberately has no download endpoint. Streaming-service
  tracks are skipped.
* "Libraries" are Music Assistant filesystem provider instances, so
  MUSIC_LIBRARIES filters by provider instance id or name.
* Playlists are created as Music Assistant library playlists (builtin provider).
"""

import logging
import os
import shutil
import time
from datetime import datetime, timezone

import config

from . import context
from . import http as requests
from .helper import detect_path_format, is_auth_error

logger = logging.getLogger(__name__)

REQUESTS_TIMEOUT = 300
MA_PAGE_SIZE = 500
MA_PLAYLIST_BATCH_SIZE = 100
MA_TASK_TIMEOUT = 120
MA_TASK_POLL_SECONDS = 0.5
_TASK_DONE = {'success'}
# partial_success: MA could not add some of the tracks, so the playlist is incomplete
_TASK_FAILED = {'failed', 'cancelled', 'canceled', 'partial_success'}
FILESYSTEM_DOMAIN_PREFIX = 'filesystem_'
LIBRARY = 'library'


class MusicAssistantError(Exception):
    """Raised for unusable Music Assistant responses."""


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #

def _creds(user_creds=None):
    return context.active_creds(user_creds) or {}


def _base_url(user_creds=None):
    creds = _creds(user_creds)
    url = creds.get('url') or getattr(config, 'MUSICASSISTANT_URL', '')
    return (url or '').rstrip('/')


def _headers(user_creds=None):
    creds = _creds(user_creds)
    token = creds.get('token') or getattr(config, 'MUSICASSISTANT_TOKEN', '')
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _music_path_setting(user_creds=None):
    creds = _creds(user_creds)
    return creds.get('music_path') or getattr(config, 'MUSICASSISTANT_MUSIC_PATH', '') or ''


def _music_roots(user_creds=None):
    """Parse ``music_path``: ``/music`` for all providers, or ``inst=/a;inst2=/b``."""
    raw = _music_path_setting(user_creds).strip()
    roots = {}
    if not raw:
        return roots
    for part in raw.split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' in part and not part.startswith('/'):
            instance, path = part.split('=', 1)
            roots[instance.strip()] = path.strip()
        else:
            roots['*'] = part
    return roots


def _call(command, args=None, user_creds=None, timeout=REQUESTS_TIMEOUT):
    """Run one Music Assistant API command and return its decoded JSON result."""
    url = f"{_base_url(user_creds)}/api"
    payload = {'message_id': 'audiomuse', 'command': command, 'args': args or {}}
    r = requests.post(url, headers=_headers(user_creds), json=payload, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as exc:
        raise MusicAssistantError(f"Music Assistant {command}: response is not JSON") from exc


def _paged(command, args, user_creds=None, page_size=None, max_items=None):
    page_size = page_size or MA_PAGE_SIZE
    collected = []
    offset = 0
    while True:
        size = page_size if max_items is None else min(page_size, max_items - len(collected))
        page = _call(command, {**args, 'limit': size, 'offset': offset}, user_creds)
        if not isinstance(page, list):
            raise MusicAssistantError(f"Music Assistant {command}: expected a list")
        collected.extend(page)
        if max_items is not None and len(collected) >= max_items:
            return collected[:max_items]
        if len(page) < size:
            return collected
        offset += len(page)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

def _name(obj):
    return obj.get('name') if isinstance(obj, dict) else None


def _filesystem_mapping(track, providers=None):
    """The filesystem provider mapping of a track (preferring the filtered providers)."""
    mappings = [
        m for m in (track.get('provider_mappings') or [])
        if isinstance(m, dict)
        and str(m.get('provider_domain', '')).startswith(FILESYSTEM_DOMAIN_PREFIX)
        and m.get('available', True)
    ]
    if providers is not None:
        # strict: a track only counts for the libraries (providers) that were selected
        mappings = [m for m in mappings if m.get('provider_instance') in providers]
    return mappings[0] if mappings else None


def _local_path(mapping, user_creds=None):
    """Absolute path of a filesystem mapping as seen by AudioMuse-AI, or None."""
    if not mapping:
        return None
    roots = _music_roots(user_creds)
    root = roots.get(mapping.get('provider_instance')) or roots.get('*')
    rel = str(mapping.get('item_id') or '').lstrip('/')
    if not root or not rel:
        return None
    return os.path.join(root, rel)


def _normalize_track(track, providers=None, user_creds=None):
    mapping = _filesystem_mapping(track, providers)
    artists = track.get('artists') or []
    album = track.get('album') if isinstance(track.get('album'), dict) else {}
    album_artists = album.get('artists') or []
    track_artist = _name(artists[0]) if artists else None
    album_artist = _name(album_artists[0]) if album_artists else None
    audio_format = (mapping or {}).get('audio_format') or {}
    duration = track.get('duration')
    path = _local_path(mapping, user_creds)
    return {
        'Id': str(track.get('item_id')),
        'Name': track.get('name'),
        'AlbumArtist': track_artist or album_artist or 'Unknown Artist',
        'OriginalAlbumArtist': album_artist,
        'ArtistId': str(artists[0].get('item_id')) if artists and isinstance(artists[0], dict)
        and artists[0].get('item_id') is not None else None,
        'Album': album.get('name'),
        'Year': album.get('year'),
        'IndexNumber': track.get('track_number') or None,
        'ParentIndexNumber': track.get('disc_number') or None,
        'Path': path or (mapping or {}).get('item_id'),
        'FilePath': path,
        'RelPath': (mapping or {}).get('item_id'),
        'ProviderInstance': (mapping or {}).get('provider_instance'),
        'Container': audio_format.get('content_type') or None,
        'DurationSeconds': float(duration) if duration else None,
    }


def _normalize_album(album):
    artists = album.get('artists') or []
    added = album.get('date_added')
    return {
        'Id': str(album.get('item_id')),
        'Name': album.get('name'),
        'AlbumArtist': (_name(artists[0]) if artists else None) or 'Unknown Artist',
        'Year': album.get('year'),
        'DateCreated': added or 0,
    }


def _is_local(track, providers=None):
    return track.get('provider') == LIBRARY and _filesystem_mapping(track, providers) is not None


# --------------------------------------------------------------------------- #
# Libraries (= Music Assistant filesystem provider instances)
# --------------------------------------------------------------------------- #

def _filesystem_providers(user_creds=None):
    providers = _call('providers', {}, user_creds)
    if not isinstance(providers, list):
        raise MusicAssistantError("Music Assistant providers: expected a list")
    return [
        {'id': p.get('instance_id'), 'name': p.get('name') or p.get('instance_id')}
        for p in providers
        if isinstance(p, dict)
        and str(p.get('domain', '')).startswith(FILESYSTEM_DOMAIN_PREFIX)
        and p.get('instance_id')
    ]


def list_libraries(user_creds=None):
    try:
        return _filesystem_providers(user_creds)
    except Exception:
        logger.exception("Music Assistant list_libraries failed")
        return []


def _target_providers(user_creds=None, force_filter=False):
    """Instance ids to scan, honouring MUSIC_LIBRARIES (see the Plex backend for force_filter)."""
    user_creds = context.active_creds(user_creds)
    providers = _filesystem_providers(user_creds)
    ids = [p['id'] for p in providers]
    if not force_filter and user_creds and context.active_server() is None:
        return ids
    names_str = context.active_libraries(getattr(config, 'MUSIC_LIBRARIES', ''))
    if not names_str or not names_str.strip():
        return ids
    wanted = {n.strip().lower() for n in names_str.split(',') if n.strip()}
    matched = [
        p['id'] for p in providers
        if p['id'].lower() in wanted or (p['name'] or '').lower() in wanted
    ]
    if not matched:
        logger.warning(
            f"Music Assistant: no filesystem provider matched MUSIC_LIBRARIES={names_str!r}; "
            "nothing will be scanned."
        )
    return matched


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #

def get_recent_albums(limit):
    try:
        providers = _target_providers()
    except Exception:
        logger.exception("Music Assistant get_recent_albums: provider lookup failed")
        return []
    if not providers:
        return []
    try:
        albums = _paged(
            'music/albums/library_items',
            {'order_by': 'timestamp_added_desc', 'provider': providers, 'summary': False},
            max_items=None if limit == 0 else max(1, limit),
        )
    except Exception:
        logger.exception("Music Assistant get_recent_albums failed")
        return []
    return [_normalize_album(a) for a in albums if isinstance(a, dict)]


def get_tracks_from_album(album_id, user_creds=None):
    user_creds = context.active_creds(user_creds)
    try:
        providers = _target_providers(user_creds)
        tracks = _call(
            'music/albums/album_tracks',
            {'item_id': str(album_id), 'provider_instance_id_or_domain': LIBRARY,
             'in_library_only': True},
            user_creds,
        )
        return [
            _normalize_track(t, providers, user_creds)
            for t in tracks or [] if isinstance(t, dict) and _is_local(t, providers)
        ]
    except Exception:
        logger.exception(f"Music Assistant get_tracks_from_album failed for album {album_id}")
        return []


def _resolve_local_path(item):
    path = item.get('FilePath')
    if path:
        return path
    track = _call(
        'music/tracks/get',
        {'item_id': str(item.get('Id') or item.get('id')),
         'provider_instance_id_or_domain': LIBRARY},
    )
    return _local_path(_filesystem_mapping(track or {}))


def _inside_roots(path):
    real = os.path.realpath(path)
    for root in _music_roots().values():
        real_root = os.path.realpath(root)
        if real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep):
            return True
    return False


def download_track(temp_dir, item):
    """Copy the track's file from the shared music folder into ``temp_dir``."""
    name = item.get('Name', 'Unknown')
    try:
        track_id = str(item.get('Id') or item.get('id') or '')
        if not track_id.isdigit():
            logger.error(f"Music Assistant download_track: invalid library id {track_id!r}")
            return None
        path = _resolve_local_path(item)
        if not path:
            logger.error(
                f"Music Assistant download_track: no local file for '{name}' ({track_id}); "
                "is MUSICASSISTANT_MUSIC_PATH set and is it a filesystem track?"
            )
            return None
        if not _inside_roots(path):
            logger.error(f"Music Assistant download_track: path outside music_path: {path}")
            return None
        extension = os.path.splitext(path)[1] or '.tmp'
        if os.sep in extension or (os.altsep and os.altsep in extension):
            extension = '.tmp'
        local_filename = os.path.join(temp_dir, f"{track_id}{extension}")
        if os.path.dirname(os.path.realpath(local_filename)) != os.path.realpath(temp_dir):
            logger.error(f"Music Assistant download_track: unsafe target {local_filename}")
            return None
        shutil.copyfile(path, local_filename)
        logger.info(f"Copied '{name}' to '{local_filename}'")
        return local_filename
    except Exception as exc:
        logger.warning(f"Failed to fetch track {name}: {exc}")
        return None


def get_all_songs(user_creds=None, apply_filter=True):
    user_creds = context.active_creds(user_creds)
    if apply_filter:
        providers = _target_providers(user_creds, force_filter=True)
    else:
        providers = [p['id'] for p in _filesystem_providers(user_creds)]
    if not providers:
        return []
    tracks = _paged(
        'music/tracks/library_items',
        {'provider': providers, 'summary': False, 'order_by': 'sort_name'},
        user_creds,
    )
    return [
        _normalize_track(t, providers, user_creds)
        for t in tracks if isinstance(t, dict) and _is_local(t, providers)
    ]


def search_albums(query, user_creds=None):
    user_creds = context.active_creds(user_creds)
    try:
        albums = _call(
            'music/albums/library_items',
            {'search': query, 'limit': 10, 'summary': False},
            user_creds,
        )
    except Exception:
        logger.exception("Music Assistant search_albums failed")
        return []
    results = []
    for album in albums or []:
        if not isinstance(album, dict):
            continue
        norm = _normalize_album(album)
        results.append({
            'id': norm['Id'],
            'name': norm['Name'],
            'artist': norm['AlbumArtist'],
            'year': norm['Year'],
            'track_count': None,
        })
    return results


def test_connection(user_creds=None):
    user_creds = context.active_creds(user_creds)
    result = {
        'ok': False, 'error': None, 'auth_failed': False,
        'sample_count': 0, 'path_format': 'none', 'warnings': [],
    }
    try:
        providers = [p['id'] for p in _filesystem_providers(user_creds)]
        if not providers:
            result['error'] = 'No local filesystem music provider found in Music Assistant.'
            return result
        tracks = _call(
            'music/tracks/library_items',
            {'provider': providers, 'summary': False, 'limit': 100},
            user_creds,
        )
        sample = [
            _normalize_track(t, providers, user_creds)
            for t in tracks or [] if isinstance(t, dict) and _is_local(t, providers)
        ]
        warnings = []
        if not _music_roots(user_creds):
            warnings.append(
                'Shared music folder (music_path) is not set: AudioMuse-AI cannot read audio.'
            )
        else:
            readable = [t for t in sample if t['FilePath'] and os.path.isfile(t['FilePath'])]
            if sample and not readable:
                warnings.append(
                    f"No sample file is readable under {_music_path_setting(user_creds)!r}; "
                    "check that the music folder is mounted into AudioMuse-AI."
                )
        result.update({
            'ok': True,
            'sample_count': len(sample),
            'path_format': detect_path_format(
                [{'Id': t['Id'], 'Path': t['Path'], 'Name': t['Name'],
                  'AlbumArtist': t['AlbumArtist']} for t in sample]
            ),
            'warnings': warnings,
        })
        return result
    except Exception as e:
        logger.warning(f"Music Assistant test_connection failed: {e}")
        result.update({'error': str(e), 'auth_failed': is_auth_error(e)})
        return result


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #

def _list_playlists(user_creds=None):
    user_creds = context.active_creds(user_creds)
    try:
        playlists = _paged('music/playlists/library_items', {'summary': False}, user_creds)
    except Exception:
        logger.exception("Music Assistant list playlists failed")
        return []
    return [
        {'Id': str(p.get('item_id')), 'Name': p.get('name')}
        for p in playlists if isinstance(p, dict) and p.get('item_id') is not None
    ]


def get_all_playlists():
    return _list_playlists()


def _find_playlist(playlist_name, user_creds=None):
    for playlist in _list_playlists(user_creds):
        if playlist.get('Name') == playlist_name:
            return playlist
    return None


def get_playlist_by_name(playlist_name, user_creds=None):
    return _find_playlist(playlist_name, user_creds)


def _delete_playlist(playlist_id, user_creds=None):
    try:
        _call('music/playlists/remove', {'item_id': str(playlist_id)}, user_creds)
        return True
    except Exception:
        logger.exception(f"Exception deleting Music Assistant playlist {playlist_id}")
        return False


def delete_playlist(playlist_id):
    return _delete_playlist(playlist_id)


def get_playlist_track_ids(playlist_id, user_creds=None):
    user_creds = context.active_creds(user_creds)
    try:
        items = _call(
            'music/playlists/playlist_tracks',
            {'item_id': str(playlist_id), 'provider_instance_id_or_domain': LIBRARY},
            user_creds,
        )
    except Exception:
        logger.exception(f"Music Assistant get_playlist_track_ids failed for {playlist_id}")
        return []
    ids = []
    for t in items or []:
        if not isinstance(t, dict) or t.get('item_id') is None:
            continue
        if t.get('provider') == LIBRARY:
            ids.append(str(t['item_id']))
            continue
        # playlist entries usually carry their real provider; map them to the library item
        try:
            lib = _call(
                'music/get_library_item',
                {'media_type': 'track', 'item_id': str(t['item_id']),
                 'provider_instance_id_or_domain': t.get('provider')},
                user_creds,
            )
        except Exception:
            logger.debug(f"Music Assistant: no library item for playlist entry {t.get('item_id')}")
            continue
        if isinstance(lib, dict) and lib.get('item_id') is not None:
            ids.append(str(lib['item_id']))
    return ids


def _create_playlist_batched(title, item_ids, user_creds=None):
    if not item_ids:
        return None
    playlist = _call('music/playlists/create_playlist', {'name': title}, user_creds)
    new_id = playlist.get('item_id') if isinstance(playlist, dict) else None
    if new_id is None:
        logger.error(f"Music Assistant: created '{title}' but the response had no item_id")
        return None
    for i in range(0, len(item_ids), MA_PLAYLIST_BATCH_SIZE):
        batch = item_ids[i : i + MA_PLAYLIST_BATCH_SIZE]
        task = _call(
            'music/playlists/add_playlist_tracks',
            {'db_playlist_id': str(new_id), 'uris': [f"library://track/{tid}" for tid in batch]},
            user_creds,
        )
        if not _wait_for_task(task, user_creds):
            logger.error(f"Music Assistant: adding tracks to playlist '{title}' failed")
            return None
    return {'Id': str(new_id), 'Name': playlist.get('name', title)}


def _wait_for_task(task, user_creds=None):
    """Wait for a Music Assistant background task; False only when it reports a failure.

    ``tasks/get`` needs the SYSTEM_READ scope. When the token lacks it (or the task is
    already gone) the result cannot be checked and the add is assumed to succeed.
    """
    task_id = task.get('id') if isinstance(task, dict) else None
    status = str(task.get('status') or '') if isinstance(task, dict) else ''
    deadline = time.monotonic() + MA_TASK_TIMEOUT
    while task_id and status not in _TASK_DONE:
        if status in _TASK_FAILED:
            return False
        if time.monotonic() > deadline:
            logger.warning(f"Music Assistant task {task_id} still {status!r}; not waiting longer")
            return True
        time.sleep(MA_TASK_POLL_SECONDS)
        try:
            info = _call('tasks/get', {'task_id': task_id}, user_creds)
        except Exception as exc:
            logger.warning(f"Music Assistant: cannot check task {task_id} ({exc}); assuming ok")
            return True
        status = str((info or {}).get('status') or '')
    return True


def create_playlist(base_name, item_ids):
    try:
        return _create_playlist_batched(base_name, list(item_ids))
    except Exception:
        logger.exception("Exception creating Music Assistant playlist '%s'", base_name)
        return None


def create_instant_playlist(playlist_name, item_ids, user_creds=None):
    user_creds = context.active_creds(user_creds)
    try:
        return _create_playlist_batched(
            f"{playlist_name.strip()}_instant", list(item_ids), user_creds
        )
    except Exception:
        logger.exception("Exception creating Music Assistant instant playlist '%s'", playlist_name)
        return None


def create_or_replace_playlist(playlist_name, item_ids, user_creds=None):
    user_creds = context.active_creds(user_creds)
    if not item_ids:
        return None
    existing = _find_playlist(playlist_name, user_creds)
    if existing and existing.get('Id') and not _delete_playlist(existing['Id'], user_creds):
        logger.error(f"Music Assistant: failed to delete existing playlist '{playlist_name}'")
        return None
    try:
        return _create_playlist_batched(playlist_name, list(item_ids), user_creds)
    except Exception:
        logger.exception(f"Music Assistant create_or_replace_playlist failed for '{playlist_name}'")
        return None


# --------------------------------------------------------------------------- #
# Listening history and lyrics
# --------------------------------------------------------------------------- #

def get_top_played_songs(limit, user_creds=None):
    user_creds = context.active_creds(user_creds)
    try:
        providers = _target_providers(user_creds)
        tracks = _paged(
            'music/tracks/library_items',
            {'order_by': 'play_count_desc', 'played_only': True, 'provider': providers,
             'summary': False},
            user_creds,
            max_items=limit if limit and limit > 0 else None,
        )
    except Exception:
        logger.exception("Music Assistant get_top_played_songs failed")
        return []
    return [
        _normalize_track(t, providers, user_creds)
        for t in tracks if isinstance(t, dict) and _is_local(t, providers)
    ]


def _epoch_to_iso(epoch):
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
            '%Y-%m-%dT%H:%M:%S.000Z'
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def get_last_played_time(item_id, user_creds=None):
    user_creds = context.active_creds(user_creds)
    try:
        track = _call(
            'music/tracks/get',
            {'item_id': str(item_id), 'provider_instance_id_or_domain': LIBRARY},
            user_creds,
        )
    except Exception:
        logger.exception(f"Music Assistant get_last_played_time failed for {item_id}")
        return None
    return _epoch_to_iso((track or {}).get('last_played'))


def get_lyrics(track_id, timeout=2.5):
    try:
        track = _call(
            'music/tracks/get',
            {'item_id': str(track_id), 'provider_instance_id_or_domain': LIBRARY},
            timeout=timeout,
        )
    except Exception:
        return None
    metadata = (track or {}).get('metadata') or {}
    text = (metadata.get('lyrics') or '').strip()
    return text or None
