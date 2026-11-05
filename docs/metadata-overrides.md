# Metadata overrides

Fix fields your server gets wrong, such as a content rating, a title or tags, and keep them fixed.

## How it works

1. Add an override, on a title's page or in `overrides.yaml`. If the title already has one, its page shows the entry filled in and **Save override** replaces it (the rest of the file, comments included, is left as it is); a title never gets a second entry from the page.
2. The **Metadata** page shows what would change. Nothing is written yet.
3. **Review and apply**: the old value is saved, the new one written and **locked** so a refresh can't undo it, and every write is checked.
4. **Restore** puts the old values back and unlocks them.

On Jellyfin only some fields can be locked (title, description, content rating, tags, genres, studios); sort title, original title, tagline and critic rating are written without a lock, so a metadata refresh in Jellyfin can undo them. The Metadata page says "the server cannot lock this field" for those, and the next apply writes them again.

If you later edit a field by hand in your server, laminator reports a **conflict** and leaves it alone.

## Fields

| Field | Notes |
|---|---|
| `rating` | Content rating; must be one your server knows. |
| `title`, `sort_title`, `original_title` | Text. |
| `overview`, `tagline` | Text, or `""` to clear. Emby can't lock taglines, so a refresh may undo one; the next apply rewrites it. |
| `critic_rating` | 0-100. Wins over the automatic score write. |
| `tags`, `genres`, `studios` | A list replaces everything; `{add: [...], remove: [...]}` changes only those names; `[]` clears. |

Poster-only settings, which never touch your server's metadata: `score`, `template`, `source`, and `overlay: false` for no badge. In the file, `score` is on the badge's 0-10 scale (`score: 6.6`); on a title's page you type it on the same 0-100 scale as the critic rating (`66`) and it is converted both ways.

## The file

```yaml
overrides:
  - match: {imdb: tt0319343}
    rating: PG-13
    sort_title: "Elf (2003)"
  - match: {imdb: tt0107290}
    title: "Jurassic Park (Fan Edit)"
    tags: [Fan Edit]
    genres: {add: [Adventure]}
    critic_rating: 80
    overlay: false
  - match: {library: Movies, title: Dumbo, year: 1941}
    original_title: "Dumbo: The Flying Elephant"
```

Match by `imdb`, `tmdb` or `tvdb` id, or by `library` + `title` (+ `year`). Add `server: emby` or `server: jellyfin` to limit an entry to one server. Edit the file in the app (**Edit overrides.yaml**), which checks it before saving and keeps the previous version. With Emby and Jellyfin both connected, the servers do not have to hold the same titles: an entry that matches nothing on one server but matches on the other is listed under "not on this server" (the command line says so too) and is not counted as a problem. An entry that matches no server at all is still reported as a problem, and so is one limited with `server:` to a server that lacks the title. A title that is in both an HD and a UHD library is changed on both copies; add `library:` to change just one.

## Command line

```
laminator meta diff                        # what would change
laminator meta apply                       # write and lock
laminator restore --what meta --item Elf   # put old values back
```
