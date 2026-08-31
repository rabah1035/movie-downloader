import os
import re
import sys
import shutil
import zipfile
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

try:
    import requests
except ImportError:
    sys.exit("❌ Missing 'requests'. Install it using: pip install requests")


DEFAULT_MOVIES_DIR = r"D:\My TV\data\media\movies"


def ensure_aria2_engine() -> str:
    """Ensures portable aria2c.exe engine exists locally."""
    exe_path = Path("aria2c.exe")
    if exe_path.exists():
        return str(exe_path)

    print("📦 Downloading portable engine ('aria2c.exe')...")
    url = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
    zip_path = Path("aria2_temp.zip")

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        with open(zip_path, "wb") as f:
            f.write(resp.content)

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            for member in zip_ref.namelist():
                if member.endswith("aria2c.exe"):
                    with zip_ref.open(member) as src, open(exe_path, "wb") as dst:
                        dst.write(src.read())
                    break

        if zip_path.exists():
            zip_path.unlink()

        print("✅ Portable engine ready!\n")
        return str(exe_path)

    except Exception as e:
        if zip_path.exists():
            zip_path.unlink()
        sys.exit(f"❌ Failed to setup download engine: {e}")


def check_disk_space(target_dir: Path, estimated_size_str: str) -> bool:
    """Checks if destination drive has enough free space."""
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        stat = shutil.disk_usage(target_dir)
        free_gb = stat.free / (1024 ** 3)

        match = re.search(r'([\d\.]+)\s*([MGT]B)', estimated_size_str, re.I)
        required_gb = 2.0
        if match:
            num = float(match.group(1))
            unit = match.group(2).upper()
            if unit == "GB":
                required_gb = num
            elif unit == "MB":
                required_gb = num / 1024.0

        if free_gb < required_gb:
            print(f"\n❌ DISK SPACE ERROR:")
            print(f"   Required Space:  ~{required_gb:.2f} GB")
            print(f"   Free Drive Space: {free_gb:.2f} GB on {target_dir.anchor}")
            return False

        print(f"💾 Storage Check: {free_gb:.2f} GB free on {target_dir.anchor}")
        return True

    except Exception:
        return True


def organize_and_clean_movie(movie_folder: Path, clean_title: str):
    """
    Finds the main video file, renames it to ONLY the clean movie title,
    and deletes extra scene clutter (.txt, .nfo, sample files, subfolders).
    """
    print("\n🧹 Cleaning and renaming movie file...")
    video_extensions = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
    video_files = []

    # Recursively search for video files
    for root, _, files in os.walk(movie_folder):
        for file in files:
            ext = Path(file).suffix.lower()
            if ext in video_extensions:
                full_path = Path(root) / file
                video_files.append((full_path, full_path.stat().st_size))

    if not video_files:
        print("⚠️ No video file found to organize.")
        return

    # Sort by size to get the actual main video (ignores tiny sample videos)
    video_files.sort(key=lambda x: x[1], reverse=True)
    main_video_path, _ = video_files[0]
    ext = main_video_path.suffix

    # Create safe target filename (e.g., "Air.mp4")
    safe_clean_name = re.sub(r'[\\/:*?<>|"]+', '', clean_title).strip()
    target_video_path = movie_folder / f"{safe_clean_name}{ext}"

    # Move/Rename video to the root of the movie folder
    if main_video_path != target_video_path:
        if target_video_path.exists():
            target_video_path.unlink()
        shutil.move(str(main_video_path), str(target_video_path))

    # Delete subdirectories and extra non-movie files
    for item in list(movie_folder.iterdir()):
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        elif item != target_video_path:
            try:
                item.unlink()
            except Exception:
                pass

    print(f"✨ Organized! Clean file saved as: {target_video_path.name}")


class IMDbResolver:
    """Silently resolves exact movie title and IMDb ID."""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

    def resolve_movie(self, raw_query: str) -> tuple[str, str, str] | None:
        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', raw_query)
        target_year = year_match.group(1) if year_match else None

        clean_title = re.sub(r'\b(19\d{2}|20\d{2})\b|\(|\)', '', raw_query).strip()
        if not clean_title:
            return None

        first_char = clean_title.lower().replace(" ", "_")[0]
        url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{quote_plus(clean_title.lower())}.json"

        try:
            resp = requests.get(url, headers=self.headers, timeout=6)
            if resp.status_code != 200:
                return None

            items = resp.json().get("d", [])
            pattern = re.compile(rf"\b{re.escape(clean_title)}\b", re.IGNORECASE)

            candidates = []
            for item in items:
                imdb_id = item.get("id", "")
                title = item.get("l", "")
                year = str(item.get("y", ""))

                if imdb_id.startswith("tt") and title and pattern.search(title):
                    candidates.append((imdb_id, title, year))

            if not candidates:
                for item in items:
                    if item.get("id", "").startswith("tt"):
                        return item["id"], item.get("l"), str(item.get("y", ""))
                return None

            if target_year:
                for cand in candidates:
                    if cand[2] == target_year:
                        return cand

            return candidates[0]

        except Exception:
            return None


class CustomIndexer:
    """Multi-source indexer with accurate title matching and quality filtering."""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

    @staticmethod
    def build_magnet_link(info_hash: str, title: str) -> str:
        trackers = [
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://open.demonii.com:1337/announce",
            "udp://tracker.openbittorrent.com:80",
            "udp://tracker.torrent.eu.org:451/announce",
            "udp://explodie.org:6969/announce",
            "udp://open.stealth.si:80/announce",
            "udp://tracker.tiny-vps.com:6969/announce",
            "udp://tracker.moeking.me:6969/announce",
            "udp://p4p.arenabg.com:1337/announce"
        ]
        tr_params = "&".join([f"tr={quote_plus(t)}" for t in trackers])
        return f"magnet:?xt=urn:btih:{info_hash}&dn={quote_plus(title)}&{tr_params}"

    def is_valid_quality(self, title: str) -> bool:
        title_lower = title.lower()
        unwanted = ["2160p", "2160", "4k", "uhd", "3d", "cam", "hdcam", "telesync", "hdts"]
        if any(re.search(rf"\b{u}\b", title_lower) for u in unwanted):
            return False
        return True

    def search_piratebay(self, title: str, year: str) -> list[dict]:
        search_term = f"{title} {year}".strip()
        url = f"https://apibay.org/q.php?q={quote_plus(search_term)}"
        results = []

        try:
            resp = requests.get(url, headers=self.headers, timeout=3)
            if resp.status_code != 200:
                return []

            torrents = resp.json()
            if not isinstance(torrents, list):
                return []

            for t in torrents:
                name = t.get("name", "")
                info_hash = t.get("info_hash", "")
                seeders = int(t.get("seeders", 0))
                size_bytes = int(t.get("size", 0))

                if not info_hash or info_hash == "0000000000000000000000000000000000000000":
                    continue

                size_gb = size_bytes / (1024 ** 3)
                if size_gb > 14.0:
                    continue

                size_str = f"{size_gb:.2f} GB"

                if self.is_valid_quality(name):
                    results.append({
                        "title": name,
                        "seeders": seeders,
                        "size": size_str,
                        "indexer": "ThePirateBay",
                        "magnet": self.build_magnet_link(info_hash, name)
                    })
        except requests.RequestException:
            pass

        return results

    def search_torrentio(self, imdb_id: str) -> list[dict]:
        url = f"https://torrentio.strem.fun/stream/movie/{imdb_id}.json"
        results = []

        try:
            resp = requests.get(url, headers=self.headers, timeout=3)
            if resp.status_code != 200:
                return []

            streams = resp.json().get("streams", [])
            for stream in streams:
                info_hash = stream.get("infoHash")
                title_raw = stream.get("title", "")
                if not info_hash:
                    continue

                lines = title_raw.split("\n")
                release_name = lines[0] if lines else "Unknown Release"

                seeders = 0
                size_str = "Unknown"
                indexer = "Torrentio"

                if len(lines) > 1:
                    details = lines[1]
                    seed_match = re.search(r'👤\s*(\d+)', details)
                    if seed_match:
                        seeders = int(seed_match.group(1))

                    size_match = re.search(r'💾\s*([\d\.]+\s*[MGT]B)', details, re.I)
                    if size_match:
                        size_str = size_match.group(1)

                    idx_match = re.search(r'⚙️\s*([^\n]+)', details)
                    if idx_match:
                        indexer = idx_match.group(1).strip()

                if self.is_valid_quality(release_name):
                    results.append({
                        "title": release_name,
                        "seeders": seeders,
                        "size": size_str,
                        "indexer": indexer,
                        "magnet": self.build_magnet_link(info_hash, release_name)
                    })
        except requests.RequestException:
            pass

        return results

    def search_yts(self, imdb_id: str) -> list[dict]:
        url = f"https://yts.mx/api/v2/list_movies.json?query_term={imdb_id}"
        results = []

        try:
            resp = requests.get(url, headers=self.headers, timeout=3)
            if resp.status_code != 200:
                return []

            movies = resp.json().get("data", {}).get("movies", [])
            if movies:
                movie = movies[0]
                for t in movie.get("torrents", []):
                    quality = t.get("quality", "")
                    if quality not in ("720p", "1080p"):
                        continue

                    info_hash = t.get("hash")
                    size = t.get("size", "N/A")
                    seeds = t.get("seeds", 0)
                    title = f"{movie.get('title')} ({movie.get('year')}) [{quality}] [YTS]"

                    if info_hash:
                        results.append({
                            "title": title,
                            "seeders": seeds,
                            "size": size,
                            "indexer": "YTS",
                            "magnet": self.build_magnet_link(info_hash, title)
                        })
        except requests.RequestException:
            pass

        return results

    def search_all(self, imdb_id: str, title: str, year: str, search_word: str) -> list[dict]:
        releases = []
        releases.extend(self.search_torrentio(imdb_id))
        releases.extend(self.search_piratebay(title, year))
        releases.extend(self.search_yts(imdb_id))

        word_pattern = re.compile(rf"\b{re.escape(search_word)}\b", re.IGNORECASE)

        filtered = []
        seen_magnets = set()

        for r in releases:
            if r["magnet"] in seen_magnets:
                continue

            normalized_title = re.sub(r'[\._\-]', ' ', r["title"])

            if word_pattern.search(normalized_title) and self.is_valid_quality(r["title"]):
                seen_magnets.add(r["magnet"])
                filtered.append(r)

        filtered.sort(key=lambda x: x["seeders"], reverse=True)
        return filtered


def print_table(releases: list[dict], max_items: int = 20):
    border  = "┌──────┬──────────────────────────────────────────────────────────────┬───────────┬────────┬──────────────┐"
    header  = "│ #    │ RELEASE TITLE (720p / 1080p ONLY)                            │ SIZE      │ SEEDS  │ SOURCE       │"
    divider = "├──────┼──────────────────────────────────────────────────────────────┼───────────┼────────┼──────────────┤"
    footer  = "└──────┴──────────────────────────────────────────────────────────────┴───────────┴────────┴──────────────┘"

    print("\n" + border)
    print(header)
    print(divider)

    for idx, r in enumerate(releases[:max_items]):
        title = (r['title'][:58] + '..') if len(r['title']) > 60 else r['title']
        size = r['size'][:9]
        seeds = str(r['seeders'])
        source = r['indexer'][:12]

        print(f"│ {idx:<4} │ {title:<60} │ {size:<9} │ {seeds:<6} │ {source:<12} │")

    print(footer + "\n")


def main():
    print("=" * 70)
    print(" 🎬 DIRECT MEDIA DOWNLOADER & ORGANIZER")
    print("=" * 70)

    aria2_exe = ensure_aria2_engine()

    # Step 1: Destination Folder Selection
    print(f"\n📁 Default Destination: {DEFAULT_MOVIES_DIR}")
    custom_path = input("Enter destination folder (or press ENTER to use default): ").strip()

    if custom_path:
        base_dest_dir = Path(custom_path)
    else:
        base_dest_dir = Path(DEFAULT_MOVIES_DIR)

    # Step 2: Search Input
    raw_query = input("\n🔍 Enter movie title (e.g. 'Air' or 'Oppenheimer'): ").strip()
    if not raw_query:
        return

    clean_search_word = re.sub(r'\b(19\d{2}|20\d{2})\b|\(|\)', '', raw_query).strip()

    print(f"\n🔎 Resolving '{raw_query}' and querying trackers...")
    resolver = IMDbResolver()
    resolved = resolver.resolve_movie(raw_query)

    if not resolved:
        print("⚠️ Movie not found on IMDb.")
        return

    imdb_id, title, year = resolved
    print(f"🎯 Matched: {title} ({year}) [{imdb_id}]")

    indexer = CustomIndexer()
    releases = indexer.search_all(imdb_id, title, year, clean_search_word)

    if not releases:
        print("⚠️ No matching 720p/1080p releases found.")
        return

    display_count = min(20, len(releases))
    print(f"✅ Found {len(releases)} release(s)! Top choices (sorted by highest seeders):")

    print_table(releases, max_items=display_count)

    try:
        rel_choice = int(input(f"Select release # (0-{display_count - 1}) to download: "))
        selected_release = releases[rel_choice]
    except (ValueError, IndexError):
        print("❌ Invalid selection.")
        return

    # Folder Structure: D:\My TV\data\media\movies\Title (Year)
    folder_name = f"{title} ({year})" if year and year != "N/A" else title
    movie_folder = base_dest_dir / folder_name

    # Check disk space on destination drive
    if not check_disk_space(movie_folder, selected_release["size"]):
        return

    magnet_link = selected_release["magnet"]

    print(f"\n🚀 Downloading: {selected_release['title']}")
    print(f"📁 Destination Folder: {movie_folder}\n")

    cmd = [
        aria2_exe,
        f"--dir={movie_folder}",
        "--seed-time=0",
        "--console-log-level=warn",
        "--summary-interval=2",
        "--bt-max-peers=120",
        "--max-connection-per-server=16",
        magnet_link
    ]

    try:
        subprocess.run(cmd, check=True)
        # Organize and rename video file cleanly after download
        organize_and_clean_movie(movie_folder, title)
        print(f"\n✅ All done! Movie ready in: {movie_folder}")
    except KeyboardInterrupt:
        print("\n\n⚠️ Download paused by user.")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Download failed (code {e.returncode})")


if __name__ == "__main__":
    main()
