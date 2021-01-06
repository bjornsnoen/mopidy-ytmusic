from urllib.parse import urlparse, parse_qs
import pykka
from mopidy import backend, httpclient
from mopidy.models import Ref, Track, Artist, Album, SearchResult, Playlist
from mopidy_ytmusic import logger
import requests
import json
import re
import random


YDL = None
API = None
TRACKS = {}

# music.youtube.com only seems to use the 5dd3f3b2 player.  So we'll just keep the
# translation matrices for that to figure out the url ourselves, instead of waiting
# for youtube-dl to do it slowly.  This also allows premium accounts to function
# correctly since we're correctly authenticated.
SIGXLATE = {
        106 : [101, 100, 99, 98, 97, 96, 95, 94, 93, 105, 91, 90, 89, 88, 87, 86, 85, 84, 83, 82, 81, 80, 79, 78, 77, 76, 75, 74, 73, 72, 71, 70, 0, 68, 67, 66, 65, 64, 63, 62, 61, 2, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 60],
        110 : [105, 104, 103, 102, 101, 100, 99, 98, 97, 109, 95, 94, 93, 92, 91, 90, 89, 88, 87, 86, 85, 84, 83, 82, 81, 80, 79, 78, 77, 76, 75, 74, 73, 72, 71, 70, 0, 68, 67, 66, 65, 64, 63, 62, 61, 2, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 60],
}

# Fallback to youtube-dl if our shit don't work.
def get_video_fallback(id_):
    uri = f"https://music.youtube.com/watch?v={id_}"
    vid = YDL.extract_info(
        url=uri,
        download=False,
        ie_key=None,
        extra_info={},
        process=True,
        force_generic_extractor=False,
    )
    for fmt in vid["formats"]:
        if fmt["ext"] == "m4a":
            logger.debug("YTMusic Stream URI %s", fmt["url"])
            return fmt["url"]
    return None

def get_video(id_):
# ytmusicapi just doesn't give us the detail we need.  So we have to re-implement their
# code to get access to the tracking URLs as well as the streaming data.
#   streams = API.get_streaming_data(id_)
    endpoint = "https://www.youtube.com/get_video_info"
    params = {"video_id": id_, "hl": API.language, "el": "detailpage", "c": "WEB_REMIX", "cver": "0.1"}
    response = requests.get(endpoint,params,headers=API.headers,proxies=API.proxies)
    text = parse_qs(response.text)
    player_response = json.loads(text['player_response'][0])
    streams = player_response['streamingData']
    url = None
    # Try to find the highest quality stream.  We want "AUDIO_QUALITY_HIGH", barring
    # that find the highest bitrate audio/mp4 stream.
    if 'adaptiveFormats' in streams:
        playstr = None
        for stream in streams['adaptiveFormats']:
            if 'audioQuality' in stream and stream['audioQuality'] == 'AUDIO_QUALITY_HIGH':
                playstr = stream
                break
        if playstr is None:  # Bummer, try for audio/mp4 of lesser quality
            bitrate = 0
            for stream in streams['adaptiveFormats']:
                if stream['mimeType'].startswith('audio/mp4') and stream['averageBitrate'] > bitrate:
                    bitrate = stream['averageBitrate']
                    playstr = stream
        if playstr is not None:
            if 'signatureCipher' in playstr:
                logger.info('Found %s stream with %d ABR for %s',playstr['audioQuality'],playstr['averageBitrate'],id_)
                sc = parse_qs(playstr['signatureCipher'])
                slen = len(sc['s'][0])
                if slen in SIGXLATE:
                    sig = ''
                    for i in SIGXLATE[slen]:
                        sig += sc['s'][0][i]
                    url = sc['url'][0] + '&sig=' + sig + '&ratebypass=yes'
                else:
                    logger.error('Unknown signature length %d for %s. Falling back to youtube-dl.',slen,id_)
            elif 'url' in playstr:
                logger.info('Found %s stream with %d ABR for %s',playstr['audioQuality'],playstr['averageBitrate'],id_)
                url = playstr['url']
            else:
                logger.error('No url for %s. Falling back to youtube-dl.',id_)
        else:
            logger.error('No mp4 audio streams found for %s. Falling back to youtube-dl.',id_)
    else:
        logger.error('No streams found for %s. Falling back to youtube-dl.',id_)
    if url is not None:
        # Let YTMusic know we're playing this track so it will be added to our history.
        trackurl = re.sub(r'plid=','list=',player_response['playbackTracking']['videostatsPlaybackUrl']['baseUrl'])
        CPN_ALPHABET = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_'
        params = {
            'cpn': ''.join((CPN_ALPHABET[random.randint(0, 256) & 63] for _ in range(0, 16))),
            'referrer': "https://music.youtube.com",
            'cbr': text['cbr'][0],
            'cbrver': text['cbrver'][0],
            'c': text['c'][0],
            'cver': text['cver'][0],
            'cos': text['cos'][0],
            'cosver': text['cosver'][0],
            'cr': text['cr'][0],
            'afmt': playstr['itag'],
            'ver': 2,
        }
        tr = requests.get(trackurl,params=params,headers=API.headers,proxies=API.proxies)
        logger.debug("%d code from '%s'",tr.status_code,tr.url)
        # Return the decoded youtube url to mopidy for playback.
        return(url)
    return get_video_fallback(id_)

def parse_uri(uri):
    components = uri.split(':')
    id_ = components[2]
    upload = (len(components) > 3 and components[3] == 'upload') or False
    return id_, upload

def playlistToTracks(pls):
    if "tracks" in pls:
        for track in pls["tracks"]:
            duration = (track['duration'] if 'duration' in track else track['length']).split(":")
            if 'artists' in track:
                artists = [Artist(
                    uri=f"ytmusic:artist:{a['id']}",
                    name=a["name"],
                    sortname=a["name"],
                    musicbrainz_id="",
                ) for a in track["artists"]]
            elif 'byline' in track:
                artists = [Artist(
                    name=track["byline"],
                    sortname=track["byline"],
                    musicbrainz_id="",
                )]
            else:
                artists = None

            if 'album' in track and track['album'] is not None:
                album = Album(
                    uri=f"ytmusic:album:{track['album']['id']}",
                    name=track["album"]["name"],
                    artists=artists,
                    num_tracks=None,
                    num_discs=None,
                    date="1999",
                    musicbrainz_id="",
                )
            else:
                album = None

            TRACKS[track["videoId"]] = Track(
                uri=f"ytmusic:track:{track['videoId']}",
                name=track["title"],
                artists=artists,
                album=album,
                composers=[],
                performers=[],
                genre="",
                track_no=None,
                disc_no=None,
                date="1999",
                length=(int(duration[0]) * 60000 + int(duration[1]) * 1000),
                bitrate=0,
                comment="",
                musicbrainz_id="",
                last_modified=None,
            )


def uploadArtistToTracks(artist):
    for track in artist:
        TRACKS[track["videoId"]] = Track(
            uri=f"ytmusic:track:{track['videoId']}",
            name=track["title"],
            artists=[Artist(
                uri=f"ytmusic:artist:{a['id']}:upload",
                name=a["name"],
                sortname=a["name"],
                musicbrainz_id="",
            ) for a in track["artist"]],
            album=Album(
                uri=f"ytmusic:album:{track['album']['id']}:upload",
                name=track["album"]["name"],
                artists=[Artist(
                    uri=f"ytmusic:artist:{a['id']}:upload",
                    name=a["name"],
                    sortname=a["name"],
                    musicbrainz_id="",
                ) for a in track["artist"]],
                num_tracks=None,
                num_discs=None,
                date="1999",
                musicbrainz_id="",
            ),
            composers=[],
            performers=[],
            genre="",
            track_no=None,
            disc_no=None,
            date="1999",
            length=None,
            bitrate=0,
            comment="",
            musicbrainz_id="",
            last_modified=None,
        )


def artistToTracks(artist):
    tracks = ("songs" in artist and "results" in artist["songs"] and artist["songs"]["results"]) or []
    ret = []
    for track in tracks:
        TRACKS[track["videoId"]] = Track(
            uri=f"ytmusic:track:{track['videoId']}",
            name=track["title"],
            artists=[Artist(
                uri=f"ytmusic:artist:{a['id']}",
                name=a["name"],
                sortname=a["name"],
                musicbrainz_id="",
            ) for a in track["artists"]],
            album=Album(
                uri=f"ytmusic:album:{track['album']['id']}",
                name=track["album"]["name"],
                artists=[Artist(
                    uri=f"ytmusic:artist:{a['id']}",
                    name=a["name"],
                    sortname=a["name"],
                    musicbrainz_id="",
                ) for a in track["artists"]],
                num_tracks=None,
                num_discs=None,
                date="1999",
                musicbrainz_id="",
            ),
            composers=[],
            performers=[],
            genre="",
            track_no=None,
            disc_no=None,
            date="1999",
            length=None,
            bitrate=0,
            comment="",
            musicbrainz_id="",
            last_modified=None,
        )
        ret.append(TRACKS[track["videoId"]])
    return(ret);


def uploadAlbumToTracks(album, id_):
    artists = [Artist(
        uri=f"ytmusic:artist:{album['artist']['id']}:upload",
        name=album["artist"]["name"],
        sortname=album["artist"]["name"],
        musicbrainz_id="",
    )]
    albumRef = Album(
        uri=f"ytmusic:album:{id_}:upload",
        name=album["title"],
        artists=artists,
        num_tracks=int(album["trackCount"]) if str(album["trackCount"]).isnumeric() else None,
        num_discs=None,
        date=f"{album['year']}",
        musicbrainz_id="",
    )
    if "tracks" in album:
        for track in album["tracks"]:
            TRACKS[track["videoId"]] = Track(
                uri=f"ytmusic:track:{track['videoId']}",
                name=track["title"],
                artists=artists,
                album=albumRef,
                composers=[],
                performers=[],
                genre="",
                track_no=None,
                disc_no=None,
                date=f"{album['year']}",
                length=None,
                bitrate=0,
                comment="",
                musicbrainz_id="",
                last_modified=None,
            )


def albumToTracks(album, id_):
    ret = []
    date = f"{album['releaseDate']['year']}"
    artists = [Artist(
        uri=f"ytmusic:artist:{artist['id']}",
        name=artist["name"],
        sortname=artist["name"],
        musicbrainz_id="",
    ) for artist in album["artist"]]
    albumObj = Album(
        uri=f"ytmusic:album:{id_}",
        name=album["title"],
        artists=artists,
        num_tracks=int(album["trackCount"]) if str(album["trackCount"]).isnumeric() else None,
        num_discs=None,
        date=date,
        musicbrainz_id="",
    )
    for song in album["tracks"]:
        track = Track(
            uri=f"ytmusic:track:{song['videoId']}",
            name=song["title"],
            artists=artists,
            album=albumObj,
            composers=[],
            performers=[],
            genre="",
            track_no=int(song["index"]) if str(song["index"]).isnumeric() else None,
            disc_no=None,
            date=date,
            length=int(song["lengthMs"]) if str(song["lengthMs"]).isnumeric() else None,
            bitrate=0,
            comment="",
            musicbrainz_id="",
            last_modified=None,
        )
        TRACKS[song["videoId"]] = track
        ret.append(track)
    return(ret)


def parseSearch(results, field=None, queries=[]):
    tracks = set()
    salbums = set()
    sartists = set()
    for result in results:
        if result["resultType"] == "song":
            if field == "track" and not any(q.casefold() == result["title"].casefold() for q in queries):
                continue
            try:
                length = [int(i) for i in result["duration"].split(":")]
            except ValueError:
                length = [0, 0]
            if result['videoId'] == None:
                continue
            track = Track(
                uri=f"ytmusic:track:{result['videoId']}",
                name=result["title"],
                artists=[Artist(
                    uri=f"ytmusic:artist:{a['id']}",
                    name=a["name"],
                    sortname=a["name"],
                    musicbrainz_id="",
                ) for a in result["artists"]],
                album=Album(
                    uri=f"ytmusic:album:{result['album']['id']}",
                    name=result["album"]["name"],
                    artists=[Artist(
                        uri=f"ytmusic:artist:{a['id']}",
                        name=a["name"],
                        sortname=a["name"],
                        musicbrainz_id="",
                    ) for a in result["artists"]],
                    num_tracks=None,
                    num_discs=None,
                    date="1999",
                    musicbrainz_id="",
                ) if "album" in result else None,
                composers=[],
                performers=[],
                genre="",
                track_no=None,
                disc_no=None,
                date="1999",
                length=(length[0] * 60 * 1000) + (length[1] * 1000),
                bitrate=0,
                comment="",
                musicbrainz_id="",
                last_modified=None,
            )
            tracks.add(track)
        elif result["resultType"] == "album":
            if field == "album" and not any(q.casefold() == result["title"].casefold() for q in queries):
                continue
            try:
                album = API.get_album(result["browseId"])
                artists = [Artist(
                    uri="",
                    name=result["artist"],
                    sortname=result["artist"],
                    musicbrainz_id="",
                )]
                date = result['year']
                albumObj = Album(
                    uri=f"ytmusic:album:{result['browseId']}",
                    name=album["title"],
                    artists=artists,
                    num_tracks=int(album["trackCount"]) if str(album["trackCount"]).isnumeric() else None,
                    num_discs=None,
                    date=date,
                    musicbrainz_id="",
                )
                salbums.add(albumObj)
            except Exception:
                logger.exception("YTMusic failed parsing album %s", result["title"])
        elif result["resultType"] == "artist":
            if field == "artist" and not any(q.casefold() == result["artist"].casefold() for q in queries):
                continue
            try:
                artistq = API.get_artist(result["browseId"])
                artist = Artist(
                    uri=f"ytmusic:artist:{result['browseId']}",
                    name=artistq["name"],
                    sortname=artistq["name"],
                    musicbrainz_id="",
                )
                sartists.add(artist)
                if 'albums' in artistq:
                    if 'params' in artistq['albums']:
                        albums = API.get_artist_albums(artistq["channelId"],artistq["albums"]["params"])
                        for album in albums:
                            albumObj = Album(
                                uri=f"ytmusic:album:{album['browseId']}",
                                name=album["title"],
                                artists=[artist],
                                date=album['year'],
                                musicbrainz_id="",
                            )
                            salbums.add(albumObj)
                    elif 'results' in artistq['albums']:
                        for album in artistq["albums"]["results"]:
                            albumObj = Album(
                                uri=f"ytmusic:album:{album['browseId']}",
                                name=album["title"],
                                artists=[artist],
                                date=album['year'],
                                musicbrainz_id="",
                            )
                            salbums.add(albumObj)
                if 'singles' in artistq and 'results' in artistq['singles']:
                    for single in artistq['singles']['results']:
                        albumObj = Album(
                            uri=f"ytmusic:album:{single['browseId']}",
                            name=single['title'],
                            artists=[artist],
                            date=single['year'],
                            musicbrainz_id="",
                        )
                        salbums.add(albumObj)
            except Exception:
                logger.exception("YTMusic failed parsing artist %s", result["artist"])
    tracks = list(tracks)
    for track in tracks:
        id_, upload = parse_uri(track.uri)
        TRACKS[id_] = track
    artists = list(sartists)
    albums = list(salbums)
#   return tracks
    logger.info("YTMusic search returned %d results", len(tracks) + len(artists) + len(albums))
    return SearchResult(
        uri="ytmusic:search",
        tracks=tracks,
        artists=artists,
        albums=albums,
    )


class YTMusicBackend(pykka.ThreadingActor, backend.Backend):
    def __init__(self, config, audio):
        super().__init__()
        self.config = config
        self.audio = audio
        self.uri_schemes = ["ytmusic"]

        from youtube_dl import YoutubeDL
        from ytmusicapi.ytmusic import YTMusic

        global YDL
        YDL = YoutubeDL({
            "format": "bestaudio/m4a/mp3/ogg/best",
            "proxy": httpclient.format_proxy(self.config["proxy"]),
            "nocheckcertificate": True,
        })
        global API
        API = YTMusic(config["ytmusic"]["auth_json"])

        self.playback = YouTubePlaybackProvider(audio=audio, backend=self)
        self.library = YouTubeLibraryProvider(backend=self)
        self.playlists = YouTubePlaylistsProvider(backend=self)


class YouTubePlaybackProvider(backend.PlaybackProvider):
    def __init__(self, audio, backend):
        super().__init__(audio, backend)
        self.last_id = None

    def translate_uri(self, uri):
        logger.info('YTMusic PlaybackProvider.translate_uri "%s"', uri)

        if "ytmusic:track:" not in uri:
            return None

        try:
            id_,upload = parse_uri(uri)
            self.last_id = id_
            return get_video(id_)
        except Exception as e:
            logger.error('translate_uri error "%s"', e)
            return None


class YouTubeLibraryProvider(backend.LibraryProvider):
    root_directory = Ref.directory(uri="ytmusic:root", name="YouTube Music")

    def browse(self, uri):
        logger.info("YTMusic browsing uri \"%s\"", uri)
        if uri == "ytmusic:root":
            return [
                Ref.directory(uri="ytmusic:artist", name="Artists"),
                Ref.directory(uri="ytmusic:album", name="Albums"),
                Ref.directory(uri="ytmusic:liked", name="Liked Songs"),
                Ref.directory(uri="ytmusic:watch", name="Similar to last played"),
            ]
        elif uri == "ytmusic:artist":
            try:
                library_artists = [
                    Ref.artist(uri=f"ytmusic:artist:{a['channelId']}", name=a["artist"])
                    for a in API.get_library_artists(limit=100)
                ]
                logger.info("YTMusic found %d artists in library", len(library_artists))
            except Exception:
                logger.exception("YTMusic failed getting artists from library")
                library_artists = []
            # try:
            #     upload_artists = [
            #         Ref.artist(uri=f"ytmusic:artist:{a['browseId']}:upload", name=a["artist"])
            #         for a in API.get_library_upload_artists(limit=100)
            #     ]
            #     logger.info("YTMusic found %d uploaded artists", len(upload_artists))
            # except Exception:
            #     logger.exception("YTMusic failed getting uploaded artists")
            #     upload_artists = []
            return library_artists  # + upload_artists
        elif uri == "ytmusic:album":
            try:
                library_albums = [
                    Ref.album(uri=f"ytmusic:album:{a['browseId']}", name=a["title"])
                    for a in API.get_library_albums(limit=100)
                ]
                logger.info("YTMusic found %d albums in library", len(library_albums))
            except Exception:
                logger.exception("YTMusic failed getting albums from library")
                library_albums = []
            # try:
            #     upload_albums = [
            #         Ref.album(uri=f"ytmusic:album:{a['browseId']}:upload", name=a["title"])
            #         for a in API.get_library_upload_albums(limit=100)
            #     ]
            #     logger.info("YTMusic found %d uploaded albums", len(upload_albums))
            # except Exception:
            #     logger.exception("YTMusic failed getting uploaded albums")
            #     upload_albums = []
            return library_albums  # + upload_albums
        elif uri == "ytmusic:liked":
            try:
                res = API.get_liked_songs(limit=100)
                playlistToTracks(res)
                logger.info("YTMusic found %d liked songs", len(res["tracks"]))
                return [
                    Ref.track(uri=f"ytmusic:track:{t['videoId']}", name=t["title"])
                    for t in ("tracks" in res and res["tracks"]) or []
                ]
            except Exception:
                logger.exception("YTMusic failed getting liked songs")
        elif uri == "ytmusic:watch":
            try:
                playback = self.backend.playback
                if playback.last_id is not None:
                    track_id = playback.last_id
                    res = API.get_watch_playlist(track_id, limit=100)
                    if 'tracks' in res:
                        logger.info("YTMusic found %d watch songs for \"%s\"", len(res["tracks"]), track_id)
                        res['tracks'].pop(0)
                        playlistToTracks(res)
                        return [
                            Ref.track(uri=f"ytmusic:video:{t['videoId']}", name=t["title"])
                            for t in res["tracks"]
                        ]
            except Exception:
                logger.exception("YTMusic failed getting watch songs")
        elif uri.startswith("ytmusic:artist:"):
            id_, upload = parse_uri(uri)
            # if upload:
            #     try:
            #         res = API.get_library_upload_artist(id_)
            #         uploadArtistToTracks(res)
            #         return [
            #             Ref.track(uri=f"ytmusic:album:{t['videoId']}", name=t["title"])
            #             for t in res
            #         ]
            #         logger.info("YTMusic found %d songs for uploaded artist \"%s\"", len(res), res[0]["artist"]["name"])
            #     except Exception:
            #         logger.exception("YTMusic failed getting tracks for uploaded artist \"%s\"", id_)
            # else:
            try:
                res = API.get_artist(id_)
                tracks =artistToTracks(res)
                logger.info("YTMusic found %d songs for artist \"%s\" in library", len(res["songs"]), res["name"])
                return(tracks)
            except Exception:
                logger.exception("YTMusic failed getting tracks for artist \"%s\"", id_)
        elif uri.startswith("ytmusic:album:"):
            id_, upload = parse_uri(uri)
            # if upload:
            #     try:
            #         res = API.get_library_upload_album(id_)
            #         uploadAlbumToTracks(res, id_)
            #         return [
            #             Ref.track(uri=f"ytmusic:track:{t['videoId']}", name=t["title"])
            #             for t in ("tracks" in res and res["tracks"]) or []
            #         ]
            #         logger.info("YTMusic found %d songs for uploaded album \"%s\"", len(res["tracks"]), res["title"])
            #     except Exception:
            #         logger.exception("YTMusic failed getting tracks for uploaded album \"%s\"", id_)
            # else:
            try:
                res = API.get_album(id_)
                tracks = albumToTracks(res, id_)
                logger.info("YTMusic found %d songs for album \"%s\" in library", len(res["tracks"]), res["title"])
                return(tracks)
            except Exception:
                logger.exception("YTMusic failed getting tracks for album \"%s\"", id_)
        return []

    def lookup(self, uri):
        id_, upload = parse_uri(uri)
        if (uri.startswith("ytmusic:album:")):
            try:
                res = API.get_album(id_)
                tracks = albumToTracks(res, id_)
                logger.info("YTMusic found %d songs for album \"%s\" in library", len(res["tracks"]), res["title"])
                return(tracks)
            except Exception:
                logger.exception("YTMusic failed getting tracks for album \"%s\"", id_)
        elif (uri.startswith("ytmusic:artist:")):
            try:
                res = API.get_artist(id_)
                tracks = artistToTracks(res)
                logger.info("YTMusic found %d songs for artist \"%s\" in library", len(res["songs"]), res["name"])
                return(tracks)
            except Exception:
                logger.exception("YTMusic failed getting tracks for artist \"%s\"", id_)
        elif (id_) in TRACKS:
            return [TRACKS[id_]]
        return []

    def get_distinct(self, field, query=None):
        ret = set()
        if field == "artist" or field == "albumartist":
            # try:
            #     uploads = API.get_library_upload_artists(limit=100)
            # except Exception:
            #     logger.exception("YTMusic failed getting uploaded artists")
            #     uploads = []
            #     pass
            try:
                library = API.get_library_artists(limit=100)
            except Exception:
                logger.exception("YTMusic failed getting artists from library")
                library = []
                pass
            # for a in uploads:
            #     ret.add(a["artist"])
            for a in library:
                ret.add(a["artist"])
        # elif field == "album":
        #     try:
        #         uploads = API.get_library_upload_albums(limit=100)
        #     except Exception:
        #         logger.exception("YTMusic failed getting uploaded albums")
        #         uploads = []
        #         pass
        #     try:
        #         library = API.get_library_albums(limit=100)
        #     except Exception:
        #         logger.exception("YTMusic failed getting albums from library")
        #         library = []
        #         pass
        #     for a in uploads:
        #         ret.add(a["title"])
        #     for a in library:
        #         ret.add(a["title"])
        return ret

    def search(self, query=None, uris=None, exact=False):
        results = []
        logger.info("YTMusic searching for %s", query)
        if "any" in query:
            try:
                res = API.search(" ".join(query["any"]), filter=None)
                results = parseSearch(res)
            except Exception:
                logger.exception("YTMusic search failed for query \"any\"=\"%s\"", " ".join(query["any"]))
        elif "track_name" in query:
            try:
                res = API.search(" ".join(query["track_name"]), filter="songs")
                if exact:
                    results = parseSearch(res, "track", query["track_name"])
                else:
                    results = parseSearch(res)
            except Exception:
                logger.exception("YTMusic search failed for query \"title\"=\"%s\"", " ".join(query["track_name"]))
        elif "albumartist" in query or "artist" in query:
            q1 = ("albumartist" in query and query["albumartist"]) or []
            q2 = ("artist" in query and query["artist"]) or []
            try:
                res = API.search(" ".join(q1 + q2), filter="artists")
                if exact:
                    results = parseSearch(res, "artist", q1 + q2)
                else:
                    results = parseSearch(res)
            except Exception:
                logger.exception("YTMusic search failed for query \"artist\"=\"%s\"", " ".join(q1 + q2))
        elif "album" in query:
            try:
                res = API.search(" ".join(query["album"]), filter="albums")
                if exact:
                    results = parseSearch(res, "album", query["album"])
                else:
                    results = parseSearch(res)
            except Exception:
                logger.exception("YTMusic search failed for query \"album\"=\"%s\"", " ".join(query["album"]))
        else:
            logger.info("YTMusic skipping search, unsupported field types \"%s\"", " ".join(query.keys()))
            return None
        return results


class YouTubePlaylistsProvider(backend.PlaylistsProvider):
    def as_list(self):
        logger.info("YTMusic getting user playlists")
        refs = []
        try:
            playlists = API.get_library_playlists(limit=100)
        except Exception:
            logger.exception("YTMusic failed getting a list of playlists")
            playlists = []
        for pls in playlists:
            refs.append(Ref.playlist(
                uri=f"ytmusic:playlist:{pls['playlistId']}", name=pls["title"],
            ))
        return refs

    def lookup(self, uri):
        id_, upload = parse_uri(uri)
        logger.info("YTMusic looking up playlist \"%s\"", id_)
        try:
            pls = API.get_playlist(id_, limit=100)
        except Exception:
            logger.exception("YTMusic playlist lookup failed")
            pls = None
        if pls:
            tracks = []
            if "tracks" in pls:
                for track in pls["tracks"]:
                    duration = track["duration"].split(":")
                    artists = [Artist(
                        uri=f"ytmusic:artist:{a['id']}",
                        name=a["name"],
                        sortname=a["name"],
                        musicbrainz_id="",
                    ) for a in track["artists"]]
                    if track["album"]:
                        album = Album(
                            uri=f"ytmusic:album:{track['album']['id']}",
                            name=track["album"]["name"],
                            artists=artists,
                            num_tracks=None,
                            num_discs=None,
                            date="1999",
                            musicbrainz_id="",
                        )
                    else:
                        album = None
                    tracks.append(Track(
                        uri=f"ytmusic:track:{track['videoId']}",
                        name=track["title"],
                        artists=artists,
                        album=album,
                        composers=[],
                        performers=[],
                        genre="",
                        track_no=None,
                        disc_no=None,
                        date="1999",
                        length=(int(duration[0]) * 60 * 1000) + (int(duration[1]) * 1000),
                        bitrate=0,
                        comment="",
                        musicbrainz_id="",
                        last_modified=None,
                    ))
            for track in tracks:
                tid, tupload = parse_uri(track.uri)
                TRACKS[tid] = track
            return Playlist(
                uri=f"ytmusic:playlist:{pls['id']}",
                name=pls["title"],
                tracks=tracks,
                last_modified=None,
            )

    def get_items(self, uri):
        id_, upload = parse_uri(uri)
        logger.info("YTMusic getting playlist items for \"%s\"", id_)
        try:
            pls = API.get_playlist(id_, limit=100)
        except Exception:
            logger.exception("YTMusic failed getting playlist items")
            pls = None
        if pls:
            refs = []
            if "tracks" in pls:
                for track in pls["tracks"]:
                    refs.append(Ref.track(uri=f"ytmusic:track:{track['videoId']}", name=track["title"]))
                    duration = track["duration"].split(":")
                    artists = [Artist(
                        uri=f"ytmusic:artist:{a['id']}",
                        name=a["name"],
                        sortname=a["name"],
                        musicbrainz_id="",
                    ) for a in track["artists"]]
                    if 'album' in track and track["album"] is not None:
                        album = Album(
                            uri=f"ytmusic:album:{track['album']['id']}",
                            name=track["album"]["name"],
                            artists=artists,
                            num_tracks=None,
                            num_discs=None,
                            date="1999",
                            musicbrainz_id="",
                        )
                    else:
                        album = None
                    TRACKS[track["videoId"]] = Track(
                        uri=f"ytmusic:track:{track['videoId']}",
                        name=track["title"],
                        artists=artists,
                        album=album,
                        composers=[],
                        performers=[],
                        genre="",
                        track_no=None,
                        disc_no=None,
                        date="1999",
                        length=(int(duration[0]) * 60 * 1000) + (int(duration[1]) * 1000),
                        bitrate=0,
                        comment="",
                        musicbrainz_id="",
                        last_modified=None,
                    )
            return refs
        return None

    def create(self, name):
        logger.info("YTMusic creating playlist \"%s\"", name)
        try:
            id_ = API.create_playlist(name, "")
        except Exception:
            logger.exception("YTMusic playlist creation failed")
            id_ = None
        if id_:
            uri = f"ytmusic:playlist:{id_}"
            logger.info("YTMusic created playlist \"%s\"", uri)
            return Playlist(
                uri=uri,
                name=name,
                tracks=[],
                last_modified=None,
            )
        return None

    def delete(self, uri):
        logger.info("YTMusic deleting playlist \"%s\"", uri)
        id_, upload = parse_uri(uri)
        try:
            API.delete_playlist(id_)
            return True
        except Exception:
            logger.exception("YTMusic failed to delete playlist")
            return False

    def refresh(self):
        pass

    def save(self, playlist):
        id_, upload = parse_uri(playlist.uri)
        logger.info("YTMusic saving playlist \"%s\" \"%s\"", playlist.name, id_)
        try:
            pls = API.get_playlist(id_, limit=100)
        except Exception:
            logger.exception("YTMusic saving playlist failed")
            return None
        oldIds = set([t["videoId"] for t in pls["tracks"]])
        newIds = set([parse_uri(p.uri)[0] for p in playlist.tracks])
        common = oldIds & newIds
        remove = oldIds ^ common
        add = newIds ^ common
        if len(remove):
            logger.debug("YTMusic removing items \"%s\" from playlist", remove)
            try:
                videos = [t for t in pls["tracks"] if t["videoId"] in remove]
                API.remove_playlist_items(id_, videos)
            except Exception:
                logger.exception("YTMusic failed removing items from playlist")
        if len(add):
            logger.debug("YTMusic adding items \"%s\" to playlist", add)
            try:
                API.add_playlist_items(id_, list(add))
            except Exception:
                logger.exception("YTMusic failed adding items to playlist")
        if pls["title"] != playlist.name:
            logger.debug("Renaming playlist to \"%s\"", playlist.name)
            try:
                API.edit_playlist(id_, title=playlist.name)
            except Exception:
                logger.exception("YTMusic failed renaming playlist")
        return playlist