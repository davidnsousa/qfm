"""New From Template: what's in ~/Templates (or the XDG templates folder) - or, until that
folder exists, qfm's own set of common files, made here. Customize copies them into the folder
to be edited; from then on the folder is the whole list."""
import os
import stat
import zipfile

from gi.repository import GLib

ODF = "urn:oasis:names:tc:opendocument:xmlns"
ODF_NAMESPACES = (f'xmlns:office="{ODF}:office:1.0" xmlns:text="{ODF}:text:1.0" xmlns:table="{ODF}:table:1.0" '
                  f'xmlns:draw="{ODF}:drawing:1.0" xmlns:style="{ODF}:style:1.0" '
                  f'xmlns:presentation="{ODF}:presentation:1.0" office:version="1.2"')


def folder():
    """The templates folder (XDG Templates, else ~/Templates) - whether it exists or not."""
    found = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_TEMPLATES)
    if not found or os.path.realpath(found) == os.path.realpath(os.path.expanduser("~")):
        found = os.path.expanduser("~/Templates")
    return found


def available():
    """[(name, source)]: the templates folder's items (source: their path), or the built-in ones
    (source: a function that writes one to a path) while there's no folder."""
    where = folder()
    if not os.path.isdir(where):
        return [(name, make) for name, make in BUILTIN]
    try:
        names = sorted((n for n in os.listdir(where) if not n.startswith(".")), key=str.casefold)
    except OSError:
        names = []
    return [(n, os.path.join(where, n)) for n in names]


def customize():
    """Create the templates folder with the built-in templates in it (not replacing any there);
    returns the folder."""
    where = folder()
    os.makedirs(where, exist_ok=True)
    for name, make in BUILTIN:
        path = os.path.join(where, name)
        if not os.path.lexists(path):
            make(path)
    return where


# -- the built-in set ------------------------------------------------------------

def _text(content, executable=False):
    def make(path):
        with open(path, "x", encoding="utf-8") as f:
            f.write(content)
        if executable:
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return make


def _odf(kind, body):
    """An empty OpenDocument file (LibreOffice): kind text / spreadsheet / presentation."""
    media = f"application/vnd.oasis.opendocument.{kind}"

    def make(path):
        manifest = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
                    'manifest:version="1.2">'
                    f'<manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="{media}"/>'
                    '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                    '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
                    '</manifest:manifest>')
        content = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-content {ODF_NAMESPACES}>'
                   f'<office:body><office:{kind}>{body}</office:{kind}></office:body></office:document-content>')
        styles = (f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-styles {ODF_NAMESPACES}>'
                  '<office:master-styles><style:master-page style:name="Default"/></office:master-styles>'
                  '</office:document-styles>')
        with zipfile.ZipFile(path, "x") as z:
            z.writestr("mimetype", media, compress_type=zipfile.ZIP_STORED)    # first, uncompressed
            z.writestr("META-INF/manifest.xml", manifest, compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("styles.xml", styles, compress_type=zipfile.ZIP_DEFLATED)
    return make


def _docx(path):
    """An empty Word document."""
    types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    document = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p/></w:body></w:document>')
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


BUILTIN = (
    ("Text file.txt", _text("")),
    ("Markdown.md", _text("# Title\n\n")),
    ("Shell script.sh", _text("#!/usr/bin/env bash\nset -euo pipefail\n\n", executable=True)),
    ("Python script.py", _text('#!/usr/bin/env python3\n"""What this script does."""\n\n\ndef main():\n    pass\n\n\n'
                               'if __name__ == "__main__":\n    main()\n', executable=True)),
    ("Web page.html", _text('<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n'
                            '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
                            '  <title>Title</title>\n</head>\n<body>\n\n</body>\n</html>\n')),
    ("Data.json", _text("{}\n")),
    ("Document.odt", _odf("text", "<text:p/>")),
    ("Spreadsheet.ods", _odf("spreadsheet", '<table:table table:name="Sheet1"><table:table-row>'
                                            '<table:table-cell/></table:table-row></table:table>')),
    ("Presentation.odp", _odf("presentation", '<draw:page draw:name="Slide 1" draw:master-page-name="Default"/>')),
    ("Word document.docx", _docx),
)
