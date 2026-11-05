# Posters and overlays

## How it works

The first time laminator touches a title, it saves the server's poster as the **original**. Every badge is drawn on that original, so a new score replaces the old one instead of stacking. **Restore** uploads the original again.

If you change a poster in your server, laminator notices on the next run and adopts it as the new original. If a "replace all images" refresh wipes the badge, it's redrawn.

A fresh or wrong data folder that meets posters which already carry laminator's badge refuses to use them as originals, so nothing stacks.

## Scores

Scores come from MDBList, on its 0-100 scale, shown as one decimal out of 10 (73 becomes 7.3). A poster only changes when the shown value changes. Which score is used is set in the [template](templates.md#score-source), or per title in [overrides](metadata-overrides.md).

**Your own score**: add `score: 9.5` (or short text like `A+`) to a title's override. It only changes the badge.

## CriticRating (optional)

With **Settings > Options > Write the score to CriticRating** on (off by default), the same score is written to the title's CriticRating field (7.3 becomes 73) and locked. A critic rating you set by hand in an override always wins. Restoring metadata puts the old value back.

## Choosing another poster

**Choose another poster...** on a title lists candidates from the image providers your server already uses (TheMovieDb, TheTVDB, fanart.tv). Preview one with your badge, then **Use...** it. The poster you replace is kept.

## Poster history

**Poster history...** shows every poster laminator has saved for a title. Go back to the previous one, the very first one, or any other. Nothing is deleted.

## Only new or changed

`apply --changed` (the scheduled quick check) only handles new titles, posters that changed on the server, and new custom posters. Run a normal apply after changing templates or overrides.
