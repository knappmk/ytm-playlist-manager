import argparse
import os
import pathlib
import sys
import time
from typing import Optional, Tuple, List, Any

from ytmusicapi import YTMusic, OAuthCredentials


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Create a playlist with top songs of every artist provieded by text file.")
    parser.add_argument("--playlistFile",
                        help="Path to the text file containing artists (+ optional playlist) information",
                        type=pathlib.Path, required=True)
    parser.add_argument("--playlistTitle", help="Title of the playlist")
    parser.add_argument("--playlistDescription", help="Description of the playlist")
    parser.add_argument("--topNSongs", help="Count of top N songs per artist", type=int, default=6)
    return parser.parse_args()


def read_provided_input_file(filepath):
    """Read the information about the playlist to create / modify from given text file.
    First two line can contain special information for playlist title (line 1) and description (line 2).

    :param str filepath: Path to file containing information about playlist
    :rtype Tuple[Optional[str], Optional[str], List[str]]: Tuple where first ist playlist title, second ist playlist
                                                            description and the list of the artists ids
    """
    title = None
    description = None
    artist_ids = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()

            # Leerzeilen und Kommentare überspringen
            if not line or line.startswith("#"):
                if line_number < 3 and line:
                    if line_number == 1:
                        title = line.lstrip("# ")
                    else:
                        description = line.lstrip("# ").replace("\\n", "\n")
                continue

            # Die eigentlichen Künstler
            artist_ids.append(line.replace("https://music.youtube.com/channel/", ""))

    return title, description, artist_ids


def create_playlist(ytm_instance, title, description):
    """Create the playlist with given name and description, if already exists modify if nesesary

    :param YTMusic ytm_instance: Instance of the YTMusic class
    :param str title: The title of the playlist
    :param str description: The description of the playlist
    :rtype str: the playlist id
    """
    playlist_browse_id: Optional[str] = None
    try:
        playlist_search = ytm_instance.search(title, filter="playlists", scope="library")
        for playlist in playlist_search:
            if playlist["title"] == title:
                playlist_browse_id = playlist["browseId"]
                break
    except KeyError as e:
        if str(e) != "'contents'" and not (" ".join(e.args)).startswith("Unable to find 'contents'"):
            raise e

    if playlist_browse_id is None:
        print("Creating playlist...")
        pl_id = ytm_instance.create_playlist(title, description)
        # give some time
        time.sleep(1)
    else:
        pl_id = playlist_browse_id[2:]
        playlist_item = ytm_instance.get_playlist(pl_id)
        if playlist_item["description"] != description:
            ytm_instance.edit_playlist(pl_id, description=description)

    if type(pl_id) is str:
        return str(pl_id)  # str() required for type spec
    else:
        print("Failed to initialize playlist", file=sys.stderr)
        print(pl_id, file=sys.stderr)
        exit(1)


def build_playlist_song_ids(ytm_instance, artist_ids, top_song_count):
    """Create the list of song (video) ids with given number of top songs per artist

    :param YTMusic ytm_instance: Instance of the YTMusic class
    :param List[str] artist_ids: List of artists ids
    :param int top_song_count: Number of top songs per artist
    :rtype List[int]: List of songs id
    """
    track_ids = []
    for artist_num, artist_id in enumerate(artist_ids):
        print("{:03d}/{:03d} artists processing - {:d} songs so far".format(
            artist_num + 1, len(artist_ids), len(track_ids)))
        try:
            artist_content = ytm_instance.get_artist(artist_id)
        except KeyError as e:
            # There is no regular artist profile on YT Music - this is a normal YT Video channel only (videos only)
            if str(e) == "'musicImmersiveHeaderRenderer'":
                user_content = ytm_instance.get_user(artist_id)
                if "results" not in user_content["videos"]:
                    print(f"Warning: Artist id '{artist_id}' has no music nor video profile!", file=sys.stderr)
                    continue
                if user_content["videos"]["browseId"] is None:
                    video_content = user_content["videos"]["results"]
                else:
                    video_content = ytm_instance.get_user_videos(artist_id, user_content["videos"]["params"])
                videos_added = 0
                for user_video in video_content:
                    if user_video["videoId"] in track_ids:
                        print("Skipping video '", user_video["title"], "' - already in track list!")
                        continue
                    track_ids.append(user_video["videoId"])
                    videos_added += 1
                    if videos_added == top_song_count:
                        break
                print(f"Artist '{user_content["name"]}': Only adding top videos!")
                continue
            else:
                print(f"Unexpected error for artists '{artist_id}':", file=sys.stderr)
                print(str(e), file=sys.stderr)
                exit(1)
        if artist_content["songs"]["browseId"] is None:
            # there are too less songs for an actual top track playlist
            if "results" in artist_content["songs"]:
                # artists might not even have a top list if there a too few songs published
                artist_top_tracks = artist_content["songs"]["results"]
            else:
                if "results" in artist_content["singles"]:
                    if artist_content["singles"]["browseId"] is not None:
                        print(f"Warning: There might be more singles for artist '{artist_content["name"]}'.", file=sys.stderr)
                        print("Implementation missing!", file=sys.stderr)
                    artist_top_tracks = []
                    for single in artist_content["singles"]["results"]:
                        album_content = ytm_instance.get_album(single["browseId"])
                        for album_track in album_content["tracks"]:
                            artist_top_tracks.append(album_track)
                else:
                    print(f"Warning: No content for artist '{artist_content["name"]}'.", file=sys.stderr)
                    print("Implementation missing!", file=sys.stderr)
                    artist_top_tracks = []
        else:
            # artist has actual top track playlist
            top_song_playlist_id = artist_content["songs"]["browseId"][2:]
            artist_top_tracks_content = ytm_instance.get_playlist(top_song_playlist_id)
            artist_top_tracks = artist_top_tracks_content["tracks"]
        track_titles = []
        tracks_added = 0
        for track in artist_top_tracks:
            if track["videoId"] in track_ids:
                print("Skipping track '", track["title"], "' - already in track list!")
                continue
            similar_track_found = False
            for added_track_title in track_titles:
                if added_track_title in track["title"] or track["title"] in added_track_title:
                    similar_track_found = True
                    print("Skipping track title '", added_track_title, "' from '", track["artists"][0]["name"])
                    break
            track_titles.append(track["title"])
            if similar_track_found:
                continue
            track_ids.append(track["videoId"])
            tracks_added += 1
            if tracks_added == top_song_count:
                break
        print(f"Artist '{artist_content["name"]}': " + str(track_titles))

    return track_ids


def synchronize_tracks_ids_to_playlist(ytm_instance, playlist_ident, tracks):
    """Add top songs of each artists to a given playlist

    :param YTMusic ytm_instance: An instance of the YTMusic class
    :param str playlist_ident: The playlist id to add songs to
    :param List[str] tracks: List of song ids
    """
    playlist_content = ytm_instance.get_playlist(playlist_ident)
    current_tracks = playlist_content["tracks"]

    track_mismatch = False
    for track_position, track in enumerate(tracks):
        if track_position >= len(current_tracks) or track != current_tracks[track_position]["videoId"]:
            track_mismatch = True
            break

    if track_mismatch and len(current_tracks) != 0:
        ytm_instance.remove_playlist_items(playlist_ident, current_tracks)

    add_tracks_content = ytm_instance.add_playlist_items(playlist_ident, tracks)
    if "status" not in add_tracks_content or "SUCCEEDED" not in add_tracks_content["status"]:
        print("Failed to add tracks to playlist!", file=sys.stderr)

    # to_be_deleted = [track for track in current_tracks if track["videoId"] not in tracks]
    # to_be_added = [track_id for track_id in tracks if track_id not in current_track_ids]

    # ytm_instance.remove_playlist_items(playlist_ident, to_be_deleted)
    # ytm_instance.add_playlist_items(playlist_ident, to_be_added)


if __name__ == "__main__":

    args = parse_arguments()

    # ytmusic = YTMusic("oauth.json", oauth_credentials=OAuthCredentials(client_id=os.environ["YTM_CLIENT_ID"],
    #                                                                    client_secret=os.environ["YTM_CLIENT_SECRET"]))
    yt_music = YTMusic("browser.json")

    playlist_title, playlist_description, artist_list = read_provided_input_file(args.playlistFile)

    seen = set()
    duplicated_artists = set()

    for artist in artist_list:
        if artist in seen:
            duplicated_artists.add(artist)
        else:
            seen.add(artist)

    if len(duplicated_artists) > 0:
        print("There are duplicated artists: ", file=sys.stderr)
        print(list(duplicated_artists), file=sys.stderr)
        exit(1)

    if args.playlistTitle:
        playlist_title = args.playlistTitle
    if args.playlistDescription:
        playlist_description = args.playlistDescription

    if (playlist_title is None or playlist_title == "" or
            playlist_description is None or playlist_description == "" or
            len(artist_list) == 0):
        print("There is no playlist title or description or the artist list is empty.", file=sys.stderr)
        exit(1)

    playlist_id = create_playlist(yt_music, playlist_title, playlist_description)
    print(f"Playlist ID: {playlist_id}")

    track_list = build_playlist_song_ids(yt_music, artist_list, args.topNSongs)

    print("Track list")
    print(track_list)

    if len(track_list) != len(set(track_list)):
        print("Warning: Duplicates in track list!", file=sys.stderr)

    synchronize_tracks_ids_to_playlist(yt_music, playlist_id, track_list)
