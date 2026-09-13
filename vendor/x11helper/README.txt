Vendored pure-Python X11 client (python-xlib + six) for FCTool's Linux preview helper.
Nothing here runs on Windows: the tree is packed into vendor/x11helper.zip and shipped so
a Proton/Wine user's native helper process can speak XRender/XDamage without pip.
Rebuild the shipped zip with: py -3.12 tools/build_x11helper_zip.py (deterministic output).
Do not edit Xlib/ or six.py: they are verbatim upstream sources (versions in VERSIONS.txt).
The packer also pulls repo-root x11_thumbs_proto.py into the zip root (single source
of truth shared with the Windows side) -- it is not duplicated into this directory.
