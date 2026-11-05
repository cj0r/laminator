# Emby behaviour notes

What was verified against a real Emby 4.10.2 server (a test item, read back every time). Useful if you want to know why laminator does something the way it does. The Jellyfin section at the end was checked against Jellyfin 10.11.11.

## Metadata

- **Write**: fetch the full item from `GET /Users/{uid}/Items/{id}` (the `/Items` list DTO is partial), change fields, `POST /Items/{id}` with the whole body. The response is 204 even when a field was ignored, so laminator **always reads back**.
- **Writable and verified**: `OfficialRating`, `OriginalTitle`, `Overview`.
- **Sort title**: `ForcedSortName` is silently ignored *unless `SortName` is in `LockedFields`*. laminator writes the value and locks `SortName` together; the item's `SortName` then follows.
- **Content rating**: Emby maps ratings by name through `GET /Localization/ParentalRatings` (G=1, PG=5, PG-13=7, R=9, NC-17=10; TV-Y to TV-MA also mapped). `GET /Items/{id}/MetadataEditor` lists the names the editor accepts. A rating not in the table has no level, so restrictions by maximum rating will not apply to it: use names from that list.
- **Locks**: `LockedFields` accepts `Name, Overview, OfficialRating, SortName, OriginalTitle, CriticRating, Cast, Genres, Tags, Studios, Runtime` (`ParentalRating` is an alias for `OfficialRating`). An unknown name is silently dropped, and one bad name made the whole list fail to apply, so laminator sends only valid names and verifies.
- **Refresh**: a "replace all metadata" refresh overwrote unlocked overrides with the server's values. With those fields in `LockedFields` it left them intact. So locking exactly the overridden fields works.
- **CriticRating**: Emby leaves it empty by default. Locked, it survives a "replace all metadata" refresh; unlocked, the refresh clears it.
- **Title, tags, genres, studios, critic rating, tagline** (verified on 4.10.2 with a read-back, then restored exactly): `Name`, `CriticRating` and `Taglines` (a list) are plain writes. **`Tags` and `Genres` are ignored unless their twin `TagItems` / `GenreItems` (objects with a `Name`) are sent too**, and `Studios` is a list of objects with a `Name`; the server assigns the ids. `Name`, `Tags`, `Genres`, `Studios` and `CriticRating` accept a lock under their own names and **survive a "replace all metadata" refresh when locked**. **`Taglines` cannot be locked** (the lock name is silently dropped), and a refresh puts the old tagline back.
- Refresh parameters: `ImageRefreshMode` and `MetadataRefreshMode` take `Default` and `FullRefresh` (not `None`); a bad value returns 400.

## Images

- A normal metadata refresh leaves an uploaded poster in place; "Replace all images" replaces it unless the item has `LockData` set (which locks all metadata, so laminator does not use it).
- `GET /Items/{id}/RemoteImages` lists candidates from TheMovieDb, TheTVDB and FanArt, with a `RatingType` (score or likes); `POST /Items/{id}/RemoteImages/Download?Type&ProviderName&ImageUrl` makes the server fetch the chosen image.
- Libraries expose `Locations`; a movie's `Path` is the file and a series' `Path` is its folder (used for exports and custom posters).
- `GET /System/Info` is used as the key test.

## Jellyfin (10.11.11)

- **Locks**: `LockedFields` only accepts `MetadataField` names: `Name, Overview, OfficialRating, Genres, Tags, Studios, Cast, Runtime, ProductionLocations`. Any other name (`SortName`, `OriginalTitle`, `CriticRating`, `Taglines`) makes the **whole item update fail with HTTP 400** (Emby silently drops it). laminator therefore writes sort title, original title and critic rating on Jellyfin **without a lock**, and a metadata refresh there can undo them; the next apply writes them again.
- **Sort title**: `ForcedSortName` is written (no lock needed); Jellyfin derives `SortName` from it and pads numbers (`Halloween 1978` reads back as `halloween 0000001978`), so laminator reads `ForcedSortName` back.
- `OfficialRating` and the other lockable fields are written and locked as on Emby, with the same whole-body `POST /Items/{id}` and read-back.
