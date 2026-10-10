import io

from PIL import Image

from app.extractor import make_thumbnail_bytes_from_bytes
from app.main import _thumbnail_response


def test_opaque_rgba_thumbnail_uses_jpeg_without_touching_original():
    source = io.BytesIO()
    Image.new("RGBA", (1600, 800), (50, 80, 130, 255)).save(source, "PNG")
    original = source.getvalue()
    result = make_thumbnail_bytes_from_bytes(original)
    assert result is not None
    with Image.open(io.BytesIO(result)) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert thumbnail.size == (1024, 512)
    assert _thumbnail_response(result).mimetype == "image/jpeg"
    assert source.getvalue() == original


def test_real_transparency_survives_thumbnail_encoding_and_correct_mime():
    source = io.BytesIO()
    Image.new("RGBA", (800, 1200), (60, 100, 170, 100)).save(source, "PNG")
    result = make_thumbnail_bytes_from_bytes(source.getvalue())
    assert result is not None
    with Image.open(io.BytesIO(result)) as thumbnail:
        assert thumbnail.format == "PNG"
        assert thumbnail.size == (683, 1024)
        assert thumbnail.getchannel("A").getextrema() == (100, 100)
    assert _thumbnail_response(result).mimetype == "image/png"


def test_decoder_shrink_preserves_exif_orientation():
    source = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (4000, 2000), (50, 80, 130)).save(source, "JPEG", exif=exif)
    result = make_thumbnail_bytes_from_bytes(source.getvalue())
    assert result is not None
    with Image.open(io.BytesIO(result)) as thumbnail:
        assert thumbnail.size == (512, 1024)
        assert thumbnail.getexif().get(274, 1) == 1
