from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]

OUTPUT_DIR = (
    ROOT
    / "frontend"
    / "public"
    / "landing"
    / "assets"
    / "images"
)

REF_DIR = (
    ROOT
    / "ref"
).resolve()


def validate_source(
    source: Path,
) -> Path:
    source = source.expanduser().resolve()

    if not source.exists():
        raise FileNotFoundError(
            f"source image does not exist: {source}"
        )

    if not source.is_file():
        raise ValueError(
            f"source is not a file: {source}"
        )

    # rules/90-forbidden.md:
    # ref/ is human-reference-only and must not
    # become an automated source dependency.
    if (
        source == REF_DIR
        or REF_DIR in source.parents
    ):
        raise ValueError(
            "ref/ is human-reference-only; "
            "landing asset generation must use "
            "an explicitly supplied external source path"
        )

    return source


def save_webp(
    image: Image.Image,
    output: Path,
    *,
    max_width: int,
    quality: int,
) -> tuple[int, int]:
    image = image.convert("RGB")

    # Never upscale source screenshots.
    if image.width > max_width:
        ratio = max_width / image.width

        image = image.resize(
            (
                max_width,
                round(image.height * ratio),
            ),
            Image.Resampling.LANCZOS,
        )

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    image.save(
        output,
        "WEBP",
        quality=quality,
        method=6,
    )

    return image.size


def mosaic_region(
    image: Image.Image,
    box: tuple[int, int, int, int],
) -> None:
    region = image.crop(box)

    tiny = region.resize(
        (
            max(8, region.width // 20),
            max(8, region.height // 20),
        ),
        Image.Resampling.BILINEAR,
    )

    pixelated = tiny.resize(
        region.size,
        Image.Resampling.NEAREST,
    )

    image.paste(
        pixelated,
        box[:2],
    )


def prepare_market(
    source: Path,
) -> tuple[int, int]:
    with Image.open(source) as image:
        return save_webp(
            image,
            OUTPUT_DIR
            / "market-workspace.webp",
            max_width=1800,
            quality=88,
        )


def prepare_monitor(
    source: Path,
) -> tuple[int, int]:
    with Image.open(source) as source_image:
        image = source_image.convert("RGB")

    width, height = image.size

    # The supplied monitor screenshot contains
    # a QR area in the lower-left region.
    qr_box = (
        round(width * 0.055),
        round(height * 0.865),
        round(width * 0.205),
        round(height * 0.955),
    )

    mosaic_region(
        image,
        qr_box,
    )

    return save_webp(
        image,
        OUTPUT_DIR
        / "intraday-monitor.webp",
        max_width=720,
        quality=90,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare PANJI public landing screenshots. "
            "Source paths must be supplied explicitly "
            "and may not live under repo ref/."
        )
    )

    parser.add_argument(
        "--market-source",
        required=True,
        type=Path,
    )

    parser.add_argument(
        "--monitor-source",
        required=True,
        type=Path,
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    market_source = validate_source(
        args.market_source
    )

    monitor_source = validate_source(
        args.monitor_source
    )

    market_size = prepare_market(
        market_source
    )

    monitor_size = prepare_monitor(
        monitor_source
    )

    print(
        "market-workspace.webp:",
        market_size,
    )

    print(
        "intraday-monitor.webp:",
        monitor_size,
    )


if __name__ == "__main__":
    main()
