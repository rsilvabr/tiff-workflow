"""tiff-workflow: TIFF to ZIP/Deflate, EXIF from the Fuji S3/S5 Pro JPEG, sRGB thumbnails,
and the wizard that runs them.

    python convert_tiff.py                     the wizard
    python convert_tiff.py compress ...        a backend (also copy-exif, thumbnails)

The modules: common (what the backends share: the command line, the settings overlay, the
tools, the gates, the moves), compress_tiff_zip, copy_exif_to_tiff, generate_thumbnails (the
three backends; each also runs as `python -m tiff_workflow.<module>`).
"""

TOOLKIT_NAME = "tiff-workflow"
#: The release. One copy: everything else reads it from here.
TOOLKIT_VERSION = "3.1.0"
TOOLKIT_URL = "https://github.com/rsilvabr/tiff-workflow"
