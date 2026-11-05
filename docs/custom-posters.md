# Custom posters

A poster you provide replaces the server's poster as the base the badge is drawn on. Handy for fan edits and titles with poor artwork.

## Where files go

In the assets folder (`<data>/assets` by default, or set it in **Settings > Custom posters**), name the poster after the title's **media folder**:

```
/media/movies/Dumbo/dumbo.mkv   →   assets/Dumbo/poster.jpg
/media/tv/Ozark/                →   assets/Ozark/poster.jpg
```

Also accepted: `assets/<Library>/<folder>/poster.jpg`, `assets/<folder>.jpg`, and for movies sharing one folder, `assets/<file name>.jpg`. JPEG, PNG or WebP.

## In the app

- **Custom posters** (on the Library page) lists matched files, files waiting to be used, and files that match nothing. **Use custom posters now** applies them.
- On a title's page, **Custom base poster** uploads, replaces or removes one.

The server's own poster stays in the [poster history](posters.md#poster-history). Removing a custom poster moves the file to the trash; it is never deleted. While a custom poster exists, it wins over posters chosen elsewhere.

Files must be real images (up to 15 MB), and a poster that already has a laminator badge is refused.
