# Export posters

**Export** makes a zip of posters arranged to sit next to your media files when extracted:

```
movies/Dumbo/dumbo.mkv       →  movies/Dumbo/poster.jpg
movies/elf.1080p.mkv         →  movies/elf.1080p-poster.jpg   (films sharing a folder)
tv/Ozark/                    →  tv/Ozark/poster.jpg
```

## Options

- **Which posters**: as they are on the server now (with badges), or the originals.
- **Library**: one, or all.
- **Top folder**: the library's folder name (extract next to your library folders), the library's name, or none (extract inside each library folder).
- **File names**: automatic, always `poster.jpg`, or always `<file>-poster.jpg`.

The zip includes a short README and a `manifest.csv` listing where each poster went. Packages are kept for 14 days. Nothing on your server changes, and existing files are only overwritten when *you* extract the zip.
