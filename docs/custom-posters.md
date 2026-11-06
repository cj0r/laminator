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

- **Custom posters** (on the Library page) lists every file in the assets folder with its title, library, file and status (*in use*, *waiting for the next run*, *unusable*, or *not matched to any title*). **Use custom posters now** applies them.
  - Choose **Posters**, **Compact** or **List**, as on the Library page (the choice is shared, and remembered). Each view shows the poster file itself, the title, the library, the file's path in the assets folder and its status.
  - **Search** looks at the title, library and file name. The **filter** narrows by status, and shows how many files are in each. Long lists are paged (25 to 500 per page).
  - Files that match nothing are listed too, so a misspelled folder name is easy to spot: filter by *Not matched to any title*.
- On a title's page, **Custom base poster** uploads, replaces or removes one.

The server's own poster stays in the [poster history](posters.md#poster-history). Removing a custom poster moves the file to the trash; it is never deleted. While a custom poster exists, it wins over posters chosen elsewhere.

Files must be real images (up to 15 MB), and a poster that already has a laminator badge is refused.
