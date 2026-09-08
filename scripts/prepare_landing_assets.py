from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

INPUT_DIR = ROOT / "ref"
OUTPUT_DIR = (
    ROOT
    / "frontend"
    / "public"
    / "landing"
    / "assets"
    / "images"
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


def save_webp(
    image: Image.Image,
    output: Path,
    *,
    max_width: int,
    quality: int = 88,
) -> None:

    image = image.convert("RGB")

    if image.width > max_width:
        ratio = max_width / image.width

        image = image.resize(
            (
                max_width,
                round(
                    image.height
                    * ratio
                ),
            ),
            Image.Resampling.LANCZOS,
        )

    image.save(
        output,
        "WEBP",
        quality=quality,
        method=6,
    )


def mosaic_region(
    image: Image.Image,
    box: tuple[
        int,
        int,
        int,
        int,
    ],
) -> None:

    region = image.crop(box)

    tiny = region.resize(
        (
            max(
                8,
                region.width // 20,
            ),
            max(
                8,
                region.height // 20,
            ),
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


def prepare_market() -> None:

    source = (
        INPUT_DIR
        / "landing-market.png"
    )

    image = Image.open(source)

    save_webp(
        image,
        OUTPUT_DIR
        / "market-workspace.webp",
        max_width=1800,
    )


def prepare_monitor() -> None:

    source = (
        INPUT_DIR
        / "landing-monitor.png"
    )

    image = (
        Image
        .open(source)
        .convert("RGB")
    )

    width, height = image.size

    # 用户提供截图中：
    # QR 位于底部左侧。
    # 使用相对坐标，
    # 防止源图被轻微缩放后失效。
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

    save_webp(
        image,
        OUTPUT_DIR
        / "intraday-monitor.webp",
        max_width=720,
        quality=90,
    )


if __name__ == "__main__":

    prepare_market()
    prepare_monitor()

    print(
        "Landing assets prepared:"
    )

    print(
        OUTPUT_DIR
        / "market-workspace.webp"
    )

    print(
        OUTPUT_DIR
        / "intraday-monitor.webp"
    )
