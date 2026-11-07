# AudioMuse-AI - https://github.com/NeptuneHub/AudioMuse-AI
# Copyright (C) 2025 NeptuneHub
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License v3.0. See the LICENSE file
# in the project root or <https://github.com/NeptuneHub/AudioMuse-AI/blob/main/LICENSE>

"""Music Assistant media-server backend adapter unit tests.

Exercises the Music Assistant provider with a mocked ``POST /api`` endpoint only;
payloads follow Music Assistant 2.10's JSON API (``{"command", "args"}`` with a
Bearer token, media item dicts with ``provider_mappings``), so no live server is
needed. Audio files live in a temporary "shared music folder".

Main Features:
* Filesystem-provider "libraries", strict MUSIC_LIBRARIES filter, pagination
* Track/album normalization with Music Assistant library ids
* File copy from the shared music folder with source and target path checks
* Playlist create/replace with background-task checks, play stats and lyrics
"""

import os
from unittest.mock import Mock, patch

import pytest
import requests

from tasks.mediaserver import context
from tasks.mediaserver import musicassistant as ma

MA_URL = 'http://music-assistant:8095'
TOKEN = 'ma-token'
FS = 'filesystem_local--abc'
SMB = 'filesystem_smb--nas'


def _mapping(rel, instance=FS, content_type='mp3'):
    return {
        'item_id': rel,
        'provider_domain': instance.split('--')[0],
        'provider_instance': instance,
        'available': True,
        'audio_format': {'content_type': content_type},
    }


ALBUM = {'item_id': '7', 'name': 'The Truth', 'year': 2000,
         'artists': [{'item_id': '70', 'name': 'Beanie Sigel'}]}


def _tracks():
    return [
        {'item_id': '1', 'provider': 'library', 'name': 'Roc the Mic', 'duration': 241,
         'track_number': 1, 'disc_number': 1, 'last_played': 1_700_000_000,
         'artists': [{'item_id': '70', 'name': 'Beanie Sigel'},
                     {'item_id': '71', 'name': 'Freeway'}],
         'album': ALBUM, 'provider_mappings': [_mapping('0/B/The Truth/01 Roc the Mic.mp3')],
         'play_count': 5, 'metadata': {'lyrics': '  Yeah  '}},
        {'item_id': '2', 'provider': 'library', 'name': 'Second', 'duration': 200,
         'artists': [{'item_id': '70', 'name': 'Beanie Sigel'}], 'album': ALBUM,
         'provider_mappings': [_mapping('1/B/second.flac', SMB, 'flac')], 'play_count': 9},
        {'item_id': '3', 'provider': 'library', 'name': 'Streaming only', 'duration': 180,
         'artists': [{'item_id': '80', 'name': 'Someone'}], 'album': None,
         'provider_mappings': [{'item_id': 'sp:1', 'provider_domain': 'spotify',
                                'provider_instance': 'spotify--x', 'available': True}],
         'play_count': 50},
    ]


class FakeMA:
    """In-memory Music Assistant answering the JSON API commands the backend uses."""

    def __init__(self):
        self.tracks = _tracks()
        self.albums = [
            dict(ALBUM, date_added='2026-01-02'),
            {'item_id': '8', 'name': 'Older', 'year': 1999, 'date_added': '2025-01-01',
             'artists': []},
        ]
        self.playlists = {'50': {'item_id': '50', 'name': 'Old_automatic', 'tracks': ['1']}}
        self.calls = []
        self.task_status = 'success'
        self.status_code = 200
        self.fail = set()
        self.next_id = 100

    def post(self, url, headers=None, json=None, timeout=None):
        assert url == f'{MA_URL}/api'
        command, args = json['command'], json.get('args') or {}
        self.calls.append((command, args))
        if headers.get('Authorization') != f'Bearer {TOKEN}':
            return _error_resp(401)
        if command in self.fail:
            return _error_resp(500)
        return _resp(self.handle(command, args))

    def handle(self, command, args):  # noqa: C901 - one branch per API command
        if command == 'providers':
            return [
                {'instance_id': FS, 'domain': 'filesystem_local', 'name': 'Local disk'},
                {'instance_id': SMB, 'domain': 'filesystem_smb', 'name': 'NAS'},
                {'instance_id': 'spotify--x', 'domain': 'spotify', 'name': 'Spotify'},
            ]
        if command == 'music/tracks/library_items':
            wanted = set(args.get('provider') or [])
            items = [t for t in self.tracks if not wanted or any(
                m['provider_instance'] in wanted for m in t['provider_mappings'])]
            if args.get('played_only'):
                items = [t for t in items if t.get('play_count')]
            if args.get('order_by') == 'play_count_desc':
                items.sort(key=lambda t: t.get('play_count', 0), reverse=True)
            return _page(items, args)
        if command == 'music/albums/library_items':
            items = self.albums
            if args.get('search'):
                items = [a for a in items if args['search'].lower() in a['name'].lower()]
            return _page(items, args)
        if command == 'music/albums/album_tracks':
            return [t for t in self.tracks if (t.get('album') or {}).get('item_id') == args['item_id']]
        if command == 'music/tracks/get':
            return next(t for t in self.tracks if t['item_id'] == args['item_id'])
        if command == 'music/playlists/library_items':
            return _page([{'item_id': p['item_id'], 'name': p['name']}
                          for p in self.playlists.values()], args)
        if command == 'music/playlists/create_playlist':
            self.next_id += 1
            pid = str(self.next_id)
            self.playlists[pid] = {'item_id': pid, 'name': args['name'], 'tracks': []}
            return {'item_id': pid, 'name': args['name']}
        if command == 'music/playlists/add_playlist_tracks':
            if self.task_status == 'success':
                self.playlists[args['db_playlist_id']]['tracks'].extend(
                    u.removeprefix('library://track/') for u in args['uris'])
            return {'id': 'task1', 'status': 'pending'}
        if command == 'tasks/get':
            return {'id': args['task_id'], 'status': self.task_status}
        if command == 'music/playlists/playlist_tracks':
            # the builtin provider returns entries with their real provider
            out = []
            for tid in self.playlists[args['item_id']]['tracks']:
                m = next(t for t in self.tracks if t['item_id'] == tid)['provider_mappings'][0]
                out.append({'item_id': m['item_id'], 'provider': m['provider_instance']})
            return out
        if command == 'music/get_library_item':
            for t in self.tracks:
                for m in t['provider_mappings']:
                    if (m['item_id'], m['provider_instance']) == (
                            args['item_id'], args['provider_instance_id_or_domain']):
                        return t
            return None
        if command == 'music/playlists/remove':
            self.playlists.pop(args['item_id'])
            return None
        raise AssertionError(f'unexpected command {command}')


def _page(items, args):
    offset = int(args.get('offset', 0))
    return items[offset:offset + int(args.get('limit', 500))]


def _resp(payload):
    r = Mock()
    r.raise_for_status = Mock()
    r.json.return_value = payload
    return r


def _error_resp(status):
    r = Mock()
    err = requests.exceptions.HTTPError(f'{status} error')
    err.response = Mock(status_code=status)
    r.raise_for_status.side_effect = err
    return r


@pytest.fixture
def music(tmp_path):
    local = tmp_path / 'music'
    (local / '0/B/The Truth').mkdir(parents=True)
    (local / '0/B/The Truth/01 Roc the Mic.mp3').write_bytes(b'ID3fake')
    nas = tmp_path / 'nas'
    (nas / '1/B').mkdir(parents=True)
    (nas / '1/B/second.flac').write_bytes(b'fLaCfake')
    return local, nas


@pytest.fixture
def fake(music):
    local, nas = music
    server = FakeMA()
    with patch('tasks.mediaserver.musicassistant.config') as cfg, \
            patch('tasks.mediaserver.musicassistant.requests.post', side_effect=server.post), \
            patch.object(ma, 'MA_TASK_POLL_SECONDS', 0):
        cfg.MUSICASSISTANT_URL = MA_URL
        cfg.MUSICASSISTANT_TOKEN = TOKEN
        cfg.MUSICASSISTANT_MUSIC_PATH = f'{FS}={local};{SMB}={nas}'
        cfg.MUSIC_LIBRARIES = ''
        server.cfg = cfg
        yield server


class TestCatalogue:
    def test_all_songs_only_local_tracks_with_library_ids(self, fake, music):
        local, nas = music
        songs = ma.get_all_songs()
        assert [s['Id'] for s in songs] == ['1', '2']
        first = songs[0]
        assert first['Name'] == 'Roc the Mic'
        assert first['AlbumArtist'] == 'Beanie Sigel'
        assert (first['Album'], first['Year']) == ('The Truth', 2000)
        assert first['FilePath'] == os.path.join(str(local), '0/B/The Truth/01 Roc the Mic.mp3')
        assert first['DurationSeconds'] == 241.0
        assert songs[1]['FilePath'] == os.path.join(str(nas), '1/B/second.flac')
        command, args = fake.calls[-1]
        assert command == 'music/tracks/library_items'
        assert args['summary'] is False and set(args['provider']) == {FS, SMB}

    def test_libraries_and_strict_filter(self, fake):
        assert ma.list_libraries() == [{'id': FS, 'name': 'Local disk'}, {'id': SMB, 'name': 'NAS'}]
        fake.cfg.MUSIC_LIBRARIES = 'NAS'
        assert [s['Id'] for s in ma.get_all_songs()] == ['2']
        assert [s['Id'] for s in ma.get_all_songs(apply_filter=False)] == ['1', '2']
        fake.cfg.MUSIC_LIBRARIES = 'Local disk'
        assert [t['Id'] for t in ma.get_tracks_from_album('7')] == ['1']

    def test_bound_server_creds_win(self, fake, music):
        local, _ = music
        fake.cfg.MUSICASSISTANT_URL = 'http://stale:1'
        server = {'server_id': 's1', 'server_type': 'musicassistant',
                  'creds': {'url': MA_URL, 'token': TOKEN, 'music_path': str(local)},
                  'music_libraries': None}
        with context.use_server(server):
            songs = ma.get_all_songs()
        assert songs[1]['FilePath'] == os.path.join(str(local), '1/B/second.flac')

    def test_pagination(self, fake):
        with patch.object(ma, 'MA_PAGE_SIZE', 1):
            assert len(ma.get_all_songs()) == 2
        offsets = [a['offset'] for c, a in fake.calls if c == 'music/tracks/library_items']
        assert offsets == [0, 1, 2]

    def test_recent_albums_and_search(self, fake):
        assert ma.get_recent_albums(1) == [{'Id': '7', 'Name': 'The Truth',
                                            'AlbumArtist': 'Beanie Sigel', 'Year': 2000,
                                            'DateCreated': '2026-01-02'}]
        assert len(ma.get_recent_albums(0)) == 2
        assert ma.search_albums('truth')[0]['id'] == '7'

    def test_catalogue_error_propagates_others_are_contained(self, fake):
        fake.fail.add('music/albums/library_items')
        assert ma.get_recent_albums(5) == []
        assert ma.search_albums('x') == []
        fake.fail.add('music/tracks/library_items')
        with pytest.raises(requests.exceptions.HTTPError):
            ma.get_all_songs()


class TestDownload:
    def test_copies_file_from_shared_folder(self, fake, tmp_path):
        temp = tmp_path / 'tmp'
        temp.mkdir()
        path = ma.download_track(str(temp), ma.get_all_songs()[0])
        assert path == str(temp / '1.mp3')
        assert open(path, 'rb').read() == b'ID3fake'
        assert ma.download_track(str(temp), {'Id': '2', 'Name': 'Second'}) == str(temp / '2.flac')

    def test_refuses_unsafe_source_and_target(self, fake, music, tmp_path):
        local, _ = music
        (tmp_path / 'secret.mp3').write_bytes(b'x')
        temp = tmp_path / 't'
        temp.mkdir()
        outside = {'Id': '9', 'Name': 'evil', 'FilePath': str(local / '..' / 'secret.mp3')}
        assert ma.download_track(str(temp), outside) is None
        item = dict(ma.get_all_songs()[0])
        for bad_id in ('../escape', '/abs', '', '1/2'):
            item['Id'] = bad_id
            assert ma.download_track(str(temp), item) is None
        assert ma.download_track(str(temp), {'Id': '3', 'Name': 'Streaming only'}) is None
        assert list(temp.iterdir()) == []

    def test_without_music_path(self, fake, tmp_path):
        fake.cfg.MUSICASSISTANT_MUSIC_PATH = ''
        item = ma.get_all_songs()[0]
        assert item['FilePath'] is None
        assert ma.download_track(str(tmp_path), item) is None


class TestConnection:
    def test_ok_and_warnings(self, fake):
        assert ma.test_connection()['ok'] is True
        assert ma.test_connection()['warnings'] == []
        fake.cfg.MUSICASSISTANT_MUSIC_PATH = '/does/not/exist'
        assert 'No sample file' in ma.test_connection()['warnings'][0]
        fake.cfg.MUSICASSISTANT_MUSIC_PATH = ''
        assert 'music_path' in ma.test_connection()['warnings'][0]

    def test_bad_token(self, fake):
        fake.cfg.MUSICASSISTANT_TOKEN = 'wrong'
        result = ma.test_connection()
        assert result['ok'] is False and result['auth_failed'] is True


class TestPlaylists:
    def test_roundtrip(self, fake):
        created = ma.create_playlist('Mix_automatic', ['1', '2'])
        assert fake.playlists[created['Id']]['tracks'] == ['1', '2']
        assert ma.get_playlist_by_name('Mix_automatic')['Id'] == created['Id']
        assert ma.get_playlist_track_ids(created['Id']) == ['1', '2']
        replaced = ma.create_or_replace_playlist('Mix_automatic', ['2'])
        assert created['Id'] not in fake.playlists
        assert fake.playlists[replaced['Id']]['tracks'] == ['2']
        assert ma.create_instant_playlist(' Chat ', ['1'])['Name'] == 'Chat_instant'
        assert ma.delete_playlist('50') is True

    @pytest.mark.parametrize('status', ['failed', 'partial_success'])
    def test_failed_background_task_is_reported(self, fake, status):
        fake.task_status = status
        assert ma.create_playlist('Broken', ['1']) is None

    def test_batches(self, fake):
        with patch.object(ma, 'MA_PLAYLIST_BATCH_SIZE', 1):
            ma.create_playlist('B', ['1', '2'])
        adds = [a['uris'] for c, a in fake.calls if c == 'music/playlists/add_playlist_tracks']
        assert adds == [['library://track/1'], ['library://track/2']]


class TestHistoryAndLyrics:
    def test_top_played_last_played_lyrics(self, fake):
        assert [t['Id'] for t in ma.get_top_played_songs(5)] == ['2', '1']
        assert ma.get_last_played_time('1') == '2023-11-14T22:13:20.000Z'
        assert ma.get_last_played_time('2') is None
        assert ma.get_lyrics('1') == 'Yeah'
        assert ma.get_lyrics('2') is None


def test_music_roots_parsing():
    with patch('tasks.mediaserver.musicassistant.config') as cfg:
        cfg.MUSICASSISTANT_MUSIC_PATH = '/music'
        assert ma._music_roots() == {'*': '/music'}
        cfg.MUSICASSISTANT_MUSIC_PATH = 'a--1=/m1; b--2=/m2;'
        assert ma._music_roots() == {'a--1': '/m1', 'b--2': '/m2'}
