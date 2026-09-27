import argparse
import difflib
import json
import pathlib
import re
import unicodedata
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup
from ytmusicapi import YTMusic
from create_festival_toplist import create_playlist, synchronize_tracks_ids_to_playlist


DEFAULT_MAPPING_FILE = pathlib.Path("ytmusic_mapping.json")

EXCLUDED_ALBUMS = {
    "others",
    "covers",
    "solos",
    "medleys",
}

ALBUM_MATCH_THRESHOLD = 0.80
TRACK_MATCH_THRESHOLD = 0.80


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Create a playlist based on a downloaded Setlist.fm setlist. "
            "YouTube Music album and track mappings are stored persistently."
        )
    )

    parser.add_argument(
        "--setlistFile",
        help="File path to a downloaded HTML file of Setlist.fm",
        type=pathlib.Path,
        required=True,
    )

    parser.add_argument(
        "--artistChannelId",
        help=(
            "YouTube Music channel ID matching the Setlist.fm artist. "
            "Required if the artist is not yet stored in the mapping database."
        ),
    )

    parser.add_argument(
        "--mappingFile",
        help="Path to the persistent YouTube Music mapping database",
        type=pathlib.Path,
        default=DEFAULT_MAPPING_FILE,
    )

    parser.add_argument(
        "--altTourName",
        help="Alternative tour name",
    )

    return parser.parse_args()


@dataclass
class Track:
    title: str
    album: str | None
    video_id: str | None = None


@dataclass
class Setlist:
    artist: str
    location: str
    date: str
    tour: str | None
    tracks: list[Track]


@dataclass
class YTMTrack:
    title: str
    video_id: str


@dataclass
class YTMAlbum:
    name: str
    browse_id: str
    tracks: list[YTMTrack]


@dataclass
class YTMArtist:
    name: str
    channel_id: str
    albums: list[YTMAlbum]
    album_map: dict[str, str]
    track_map: dict[str, str]


def parse_setlist_html(html_path: str | pathlib.Path) -> Setlist:
    html_path = pathlib.Path(html_path)

    if not html_path.is_file():
        raise FileNotFoundError(f"HTML file was not found: {html_path}")

    with html_path.open("r", encoding="utf-8") as file:
        soup = BeautifulSoup(file, "html.parser")

    info_container = soup.select_one("div.infoContainer")

    if info_container is None:
        raise ValueError("div.infoContainer was not found.")

    # Artist and location
    headline = info_container.select_one("div.setlistHeadline")

    if headline is None:
        raise ValueError("div.setlistHeadline was not found.")

    artist_element = headline.select_one("strong a")

    if artist_element is None:
        raise ValueError("Artist could not be found.")

    artist = artist_element.get_text(strip=True)

    location_element = headline.select_one("span a")

    if location_element is None:
        raise ValueError("Location could not be found.")

    location_text = location_element.get_text(strip=True)

    location_parts = [
        part.strip()
        for part in location_text.split(",")
    ]

    if len(location_parts) < 3:
        raise ValueError(
            f"Unexpected location format: {location_text!r}"
        )

    city = location_parts[-2]
    country = location_parts[-1]

    location = f"{country}, {city}"

    # Date
    date_element = soup.select_one("div.date")

    if date_element is None:
        raise ValueError("div.date was not found.")

    month_element = date_element.select_one("span.month")
    day_element = date_element.select_one("span.day")
    year_element = date_element.select_one("span.year")

    if month_element is None:
        raise ValueError("Date month could not be found.")

    if day_element is None:
        raise ValueError("Date day could not be found.")

    if year_element is None:
        raise ValueError("Date year could not be found.")

    month_name = month_element.get_text(strip=True)
    day = day_element.get_text(strip=True)
    year = year_element.get_text(strip=True)

    month_numbers = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }

    if month_name not in month_numbers:
        raise ValueError(f"Unknown month: {month_name!r}")

    try:
        day_number = int(day)
        year_number = int(year)
    except ValueError as error:
        raise ValueError(
            f"Invalid date values: day={day!r}, year={year!r}"
        ) from error

    month_number = month_numbers[month_name]

    date = (
        f"{day_number:02d}."
        f"{month_number:02d}."
        f"{year_number:04d}"
    )

    # Tour
    tour = None

    tour_paragraph = info_container.find(
        "p",
        recursive=False,
    )

    if tour_paragraph is not None:
        tour_spans = tour_paragraph.find_all(
            "span",
            recursive=False,
        )

        if len(tour_spans) >= 2:
            tour_element = tour_spans[1].select_one("a")

            if tour_element is not None:
                tour = tour_element.get_text(strip=True)

    # Build track -> album mapping
    album_by_track: dict[str, str] = {}

    album_stats = soup.select_one("div.setlistAlbumStats")

    if album_stats is not None:
        album_list = album_stats.select_one(
            "div.col-xs-12.col-md-6 > ul"
        )

        if album_list is not None:
            album_entries = album_list.find_all(
                "li",
                recursive=False,
            )

            for album_entry in album_entries:
                album_divs = album_entry.find_all(
                    "div",
                    recursive=False,
                )

                if len(album_divs) < 2:
                    continue

                album_element = album_divs[0].select_one("a")

                if album_element is None:
                    continue

                album_name = album_element.get_text(strip=True)

                track_elements = album_divs[1].select(
                    "ul > li > a"
                )

                for track_element in track_elements:
                    track_name = track_element.get_text(strip=True)

                    if track_name:
                        album_by_track[track_name] = album_name

    # Tracks
    setlist_container = soup.select_one("div.setlistList")

    if setlist_container is None:
        raise ValueError("div.setlistList was not found.")

    tracks: list[Track] = []

    song_elements = setlist_container.select(
        "li.setlistParts.song div.songPart"
    )

    for song_element in song_elements:
        title = song_element.get_text(strip=True)

        if not title:
            continue

        tracks.append(
            Track(
                title=title,
                album=album_by_track.get(title),
            )
        )

    return Setlist(
        artist=artist,
        location=location,
        date=date,
        tour=tour,
        tracks=tracks,
    )


def load_mapping_database(
    mapping_file: pathlib.Path,
) -> dict[str, YTMArtist]:
    if not mapping_file.is_file():
        return {}

    try:
        with mapping_file.open("r", encoding="utf-8") as file:
            raw_data = json.load(file)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Mapping file contains invalid JSON: {mapping_file}"
        ) from error

    artists: dict[str, YTMArtist] = {}

    for artist_name, artist_data in raw_data.items():
        albums: list[YTMAlbum] = []

        for album_data in artist_data.get("albums", []):
            tracks = [
                YTMTrack(
                    title=track_data["title"],
                    video_id=track_data["video_id"],
                )
                for track_data in album_data.get("tracks", [])
            ]

            albums.append(
                YTMAlbum(
                    name=album_data["name"],
                    browse_id=album_data["browse_id"],
                    tracks=tracks,
                )
            )

        artists[artist_name] = YTMArtist(
            name=artist_name,
            channel_id=artist_data["channel_id"],
            albums=albums,
            album_map=artist_data.get("album_map", {}),
            track_map=artist_data.get("track_map", {}),
        )

    return artists


def save_mapping_database(
    mapping_file: pathlib.Path,
    artists: dict[str, YTMArtist],
):
    mapping_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    data = {}

    for artist_name, artist in artists.items():
        artist_data = asdict(artist)

        # The artist name is already represented by the top-level key.
        artist_data.pop("name")

        data[artist_name] = artist_data

    temporary_file = mapping_file.with_suffix(
        mapping_file.suffix + ".tmp"
    )

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )

    temporary_file.replace(mapping_file)


def get_or_create_artist(
    setlist: Setlist,
    artists: dict[str, YTMArtist],
    artist_channel_id: str | None,
    mapping_file: pathlib.Path,
) -> YTMArtist:
    existing_artist = artists.get(setlist.artist)

    if existing_artist is not None:
        if (
            artist_channel_id is not None
            and artist_channel_id != existing_artist.channel_id
        ):
            raise ValueError(
                "The provided artist channel ID does not match "
                "the channel ID stored in the mapping database. "
                f"Stored: {existing_artist.channel_id}, "
                f"provided: {artist_channel_id}"
            )

        return existing_artist

    if not artist_channel_id:
        raise ValueError(
            f"Artist {setlist.artist!r} is not yet present in the "
            "mapping database. Please provide --artistChannelId."
        )

    artist = YTMArtist(
        name=setlist.artist,
        channel_id=artist_channel_id,
        albums=[],
        album_map={},
        track_map={},
    )

    artists[setlist.artist] = artist

    save_mapping_database(
        mapping_file,
        artists,
    )

    return artist


def get_artist_albums(
    ytm_instance: YTMusic,
    artist_id: str,
):
    artist_content = ytm_instance.get_artist(artist_id)

    if "albums" not in artist_content:
        raise ValueError(
            "The provided YouTube Music artist has no albums."
        )

    if "params" in artist_content["albums"]:
        return ytm_instance.get_artist_albums(
            channelId=artist_content["albums"]["browseId"],
            params=artist_content["albums"]["params"],
        )

    return artist_content["albums"]["results"]


def get_album_tracks(
    ytm_instance: YTMusic,
    album_id: str,
):
    album_content = ytm_instance.get_album(album_id)

    if "tracks" not in album_content:
        raise ValueError(
            f"YouTube Music album {album_id!r} has no tracks."
        )

    tracks = album_content["tracks"]
    other_versions = album_content.get("other_versions")

    return tracks, other_versions


def find_album_by_browse_id(
    artist: YTMArtist,
    browse_id: str,
) -> YTMAlbum | None:
    for album in artist.albums:
        if album.browse_id == browse_id:
            return album

    return None


def load_artist_albums(
    ytm_instance: YTMusic,
    artist: YTMArtist,
    artists: dict[str, YTMArtist],
    mapping_file: pathlib.Path,
):
    if artist.albums:
        return

    print(
        f"Loading albums for artist {artist.name!r} "
        "from YouTube Music..."
    )

    albums_content = get_artist_albums(
        ytm_instance,
        artist.channel_id,
    )

    for album_content in albums_content:
        browse_id = album_content.get("browseId")
        title = album_content.get("title")

        if not browse_id or not title:
            continue

        existing_album = find_album_by_browse_id(
            artist,
            browse_id,
        )

        if existing_album is not None:
            continue

        artist.albums.append(
            YTMAlbum(
                name=title,
                browse_id=browse_id,
                tracks=[],
            )
        )

    save_mapping_database(
        mapping_file,
        artists,
    )


def normalise_title(value: str) -> str:
    value = unicodedata.normalize(
        "NFKD",
        value,
    )

    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )

    value = value.casefold()
    value = value.replace("&", " and ")

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return " ".join(value.split())


def similarity(
    first: str,
    second: str,
) -> float:
    first_normalised = normalise_title(first)
    second_normalised = normalise_title(second)

    if first_normalised == second_normalised:
        return 1.0

    return difflib.SequenceMatcher(
        None,
        first_normalised,
        second_normalised,
    ).ratio()


def find_album_candidates(
    setlist_album_name: str,
    albums: list[YTMAlbum],
) -> list[YTMAlbum]:
    normalised_setlist_album = normalise_title(
        setlist_album_name
    )

    exact_matches = [
        album
        for album in albums
        if normalise_title(album.name)
        == normalised_setlist_album
    ]

    if exact_matches:
        return exact_matches

    candidates = []

    for album in albums:
        score = similarity(
            setlist_album_name,
            album.name,
        )

        if score >= ALBUM_MATCH_THRESHOLD:
            candidates.append(
                (
                    score,
                    album,
                )
            )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [
        album
        for _, album in candidates
    ]


def find_track_candidates(
    setlist_track_name: str,
    tracks: list[YTMTrack],
) -> list[YTMTrack]:
    normalised_setlist_track = normalise_title(
        setlist_track_name
    )

    exact_matches = [
        track
        for track in tracks
        if normalise_title(track.title)
        == normalised_setlist_track
    ]

    if exact_matches:
        return exact_matches

    candidates = []

    for track in tracks:
        score = similarity(
            setlist_track_name,
            track.title,
        )

        if score >= TRACK_MATCH_THRESHOLD:
            candidates.append(
                (
                    score,
                    track,
                )
            )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [
        track
        for _, track in candidates
    ]


def load_album_tracks(
    ytm_instance: YTMusic,
    artist: YTMArtist,
    album: YTMAlbum,
    artists: dict[str, YTMArtist],
    mapping_file: pathlib.Path,
):
    if album.tracks:
        return None

    print(
        f"Loading tracks for album {album.name!r}..."
    )

    tracks_content, other_versions = get_album_tracks(
        ytm_instance,
        album.browse_id,
    )

    album.tracks = []

    for track_content in tracks_content:
        title = track_content.get("title")
        video_id = track_content.get("videoId")

        if not title or not video_id:
            continue

        album.tracks.append(
            YTMTrack(
                title=title,
                video_id=video_id,
            )
        )

    save_mapping_database(
        mapping_file,
        artists,
    )

    return other_versions


def ask_for_album_browse_id(
    setlist_album_name: str,
    candidates: list[YTMAlbum],
) -> str | None:
    print()
    print(
        f"No unique YouTube Music album could be determined "
        f"for Setlist.fm album {setlist_album_name!r}."
    )

    if candidates:
        print("Possible albums:")

        for album in candidates:
            print(
                f"  - {album.name} "
                f"[browseId: {album.browse_id}]"
            )

    browse_id = input(
        "Enter the YouTube Music album browseId "
        "or press Enter to skip album matching: "
    ).strip()

    if not browse_id:
        print(
            f"Skipping album matching for "
            f"{setlist_album_name!r}."
        )
        return None

    return browse_id


def ask_for_other_version(
    album: YTMAlbum,
    other_versions: list[dict],
) -> str:
    print()
    print(
        f"Other versions are available for album "
        f"{album.name!r}:"
    )

    choices = [
        (
            album.name,
            album.browse_id,
        )
    ]

    for version in other_versions:
        title = version.get("title")
        browse_id = version.get("browseId")

        if not title or not browse_id:
            continue

        choices.append(
            (
                title,
                browse_id,
            )
        )

    for index, (title, browse_id) in enumerate(
        choices,
        start=1,
    ):
        print(
            f"  {index}. {title} "
            f"[browseId: {browse_id}]"
        )

    while True:
        selection = input(
            f"Select album version [1-{len(choices)}]: "
        ).strip()

        try:
            index = int(selection)
        except ValueError:
            print("Please enter a valid number.")
            continue

        if 1 <= index <= len(choices):
            return choices[index - 1][1]

        print("Please enter a valid selection.")


def ensure_album_known(
    artist: YTMArtist,
    browse_id: str,
    fallback_name: str,
    artists: dict[str, YTMArtist],
    mapping_file: pathlib.Path,
) -> YTMAlbum:
    album = find_album_by_browse_id(
        artist,
        browse_id,
    )

    if album is not None:
        return album

    album = YTMAlbum(
        name=fallback_name,
        browse_id=browse_id,
        tracks=[],
    )

    artist.albums.append(album)

    save_mapping_database(
        mapping_file,
        artists,
    )

    return album


def resolve_album(
    ytm_instance: YTMusic,
    setlist_album_name: str,
    artist: YTMArtist,
    artists: dict[str, YTMArtist],
    mapping_file: pathlib.Path,
) -> YTMAlbum | None:
    mapped_browse_id = artist.album_map.get(
        setlist_album_name
    )

    if mapped_browse_id:
        return ensure_album_known(
            artist=artist,
            browse_id=mapped_browse_id,
            fallback_name=setlist_album_name,
            artists=artists,
            mapping_file=mapping_file,
        )

    load_artist_albums(
        ytm_instance=ytm_instance,
        artist=artist,
        artists=artists,
        mapping_file=mapping_file,
    )

    candidates = find_album_candidates(
        setlist_album_name,
        artist.albums,
    )

    if len(candidates) == 1:
        album = candidates[0]

        print(
            f"Matched Setlist.fm album "
            f"{setlist_album_name!r} to "
            f"YouTube Music album {album.name!r}."
        )
    else:
        browse_id = ask_for_album_browse_id(
            setlist_album_name,
            candidates,
        )

        if browse_id is None:
            return None

        album = ensure_album_known(
            artist=artist,
            browse_id=browse_id,
            fallback_name=setlist_album_name,
            artists=artists,
            mapping_file=mapping_file,
        )

    other_versions = load_album_tracks(
        ytm_instance=ytm_instance,
        artist=artist,
        album=album,
        artists=artists,
        mapping_file=mapping_file,
    )

    if other_versions:
        selected_browse_id = ask_for_other_version(
            album,
            other_versions,
        )

        if selected_browse_id != album.browse_id:
            selected_version = None

            for version in other_versions:
                if version.get("browseId") == selected_browse_id:
                    selected_version = version
                    break

            if selected_version is None:
                raise ValueError(
                    "Selected album version could not be found."
                )

            album = ensure_album_known(
                artist=artist,
                browse_id=selected_browse_id,
                fallback_name=selected_version.get(
                    "title",
                    setlist_album_name,
                ),
                artists=artists,
                mapping_file=mapping_file,
            )

            load_album_tracks(
                ytm_instance=ytm_instance,
                artist=artist,
                album=album,
                artists=artists,
                mapping_file=mapping_file,
            )

    artist.album_map[setlist_album_name] = album.browse_id

    save_mapping_database(
        mapping_file,
        artists,
    )

    return album


def ask_for_track(
    setlist_track_name: str,
    candidates: list[YTMTrack],
) -> YTMTrack:
    print()
    print(
        f"Multiple YouTube Music tracks match "
        f"{setlist_track_name!r}:"
    )

    for index, track in enumerate(
        candidates,
        start=1,
    ):
        print(
            f"  {index}. {track.title} "
            f"[videoId: {track.video_id}]"
        )

    while True:
        selection = input(
            f"Select track [1-{len(candidates)}]: "
        ).strip()

        try:
            index = int(selection)
        except ValueError:
            print("Please enter a valid number.")
            continue

        if 1 <= index <= len(candidates):
            return candidates[index - 1]

        print("Please enter a valid selection.")


def ask_for_video_id(
    track: Track,
) -> str | None:
    print()
    print(
        f"No matching YouTube Music track could be found "
        f"for {track.title!r}."
    )

    if track.album:
        print(
            f"Setlist.fm album: {track.album}"
        )

    video_id = input(
        "Enter the YouTube Music videoId "
        "or press Enter to skip this track: "
    ).strip()

    if not video_id:
        print(
            f"Skipping track {track.title!r}."
        )
        return None

    return video_id


def resolve_track(
    ytm_instance: YTMusic,
    track: Track,
    artist: YTMArtist,
    artists: dict[str, YTMArtist],
    mapping_file: pathlib.Path,
) -> str | None:
    mapped_video_id = artist.track_map.get(
        track.title
    )

    if mapped_video_id:
        track.video_id = mapped_video_id
        return mapped_video_id

    if (
        track.album is None
        or normalise_title(track.album)
        in EXCLUDED_ALBUMS
    ):
        video_id = ask_for_video_id(track)

        if video_id is None:
            return None

        artist.track_map[track.title] = video_id
        track.video_id = video_id

        save_mapping_database(
            mapping_file,
            artists,
        )

        return video_id

    album = resolve_album(
        ytm_instance=ytm_instance,
        setlist_album_name=track.album,
        artist=artist,
        artists=artists,
        mapping_file=mapping_file,
    )

    # Album matching was skipped by the user.
    if album is None:
        video_id = ask_for_video_id(track)

        if video_id is None:
            return None

        artist.track_map[track.title] = video_id
        track.video_id = video_id

        save_mapping_database(
            mapping_file,
            artists,
        )

        return video_id

    load_album_tracks(
        ytm_instance=ytm_instance,
        artist=artist,
        album=album,
        artists=artists,
        mapping_file=mapping_file,
    )

    candidates = find_track_candidates(
        track.title,
        album.tracks,
    )

    if len(candidates) == 1:
        selected_track = candidates[0]

        print(
            f"Matched {track.title!r} to "
            f"{selected_track.title!r} "
            f"on album {album.name!r}."
        )

        video_id = selected_track.video_id

    elif len(candidates) > 1:
        selected_track = ask_for_track(
            track.title,
            candidates,
        )

        video_id = selected_track.video_id

    else:
        video_id = ask_for_video_id(track)

        if video_id is None:
            return None

    artist.track_map[track.title] = video_id
    track.video_id = video_id

    save_mapping_database(
        mapping_file,
        artists,
    )

    return video_id


def resolve_setlist_tracks(
    ytm_instance: YTMusic,
    setlist: Setlist,
    artist: YTMArtist,
    artists: dict[str, YTMArtist],
    mapping_file: pathlib.Path,
) -> list[str]:
    video_ids: list[str] = []

    print()
    print("Resolving tracks:")

    for index, track in enumerate(
        setlist.tracks,
        start=1,
    ):
        print()
        print(
            f"[{index}/{len(setlist.tracks)}] "
            f"{track.title}"
        )

        video_id = resolve_track(
            ytm_instance=ytm_instance,
            track=track,
            artist=artist,
            artists=artists,
            mapping_file=mapping_file,
        )

        if video_id is not None:
            video_ids.append(video_id)

    return video_ids


def print_setlist(setlist: Setlist):
    print(f"Artist:   {setlist.artist}")
    print(f"Location: {setlist.location}")
    print(f"Date:     {setlist.date}")
    print(f"Tour:     {setlist.tour}")
    print("Tracks:")

    for track in setlist.tracks:
        print(
            f"  - {track.title} "
            f"[Album: {track.album}]"
        )


def main():
    args = parse_arguments()

    setlist = parse_setlist_html(
        args.setlistFile
    )

    if args.altTourName:
        setlist.tour = args.altTourName

    print_setlist(setlist)

    artists = load_mapping_database(
        args.mappingFile
    )

    artist = get_or_create_artist(
        setlist=setlist,
        artists=artists,
        artist_channel_id=args.artistChannelId,
        mapping_file=args.mappingFile,
    )

    yt_music = YTMusic("browser.json")

    video_ids = resolve_setlist_tracks(
        ytm_instance=yt_music,
        setlist=setlist,
        artist=artist,
        artists=artists,
        mapping_file=args.mappingFile,
    )

    print()
    print("Resolved video IDs:")

    for video_id in video_ids:
        print(f"  - {video_id}")

    skipped_tracks = [
        track
        for track in setlist.tracks
        if track.video_id is None
    ]

    print()
    print(
        f"Resolved {len(video_ids)} of "
        f"{len(setlist.tracks)} tracks successfully."
    )

    if skipped_tracks:
        print("Skipped tracks:")

        for track in skipped_tracks:
            print(f"  - {track.title}")

    print()
    print("Creating playlist on YouTube Music")

    playlist_id = create_playlist(
        ytm_instance=yt_music,
        title=f"[Setlist] {setlist.artist} - {setlist.tour}",
        description=f"{setlist.location}\n{setlist.date}",
    )

    print()
    print("Synchronizing tracks to playlist")

    synchronize_tracks_ids_to_playlist(
        ytm_instance=yt_music,
        playlist_ident=playlist_id,
        tracks=video_ids,
    )


if __name__ == "__main__":
    main()
