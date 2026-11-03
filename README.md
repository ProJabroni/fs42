# fs42

One-command lineup builder for [FieldStation42](https://github.com/shane-mason/FieldStation42).

Sets up nine channels (Classic Cartoons, Adult Animation, Anime, Animals,
Classic Movies, Sitcoms, Game Shows, Music Videos, Crime & Mystery), fills
them with public-domain content from archive.org sized to your free disk
space, gives each channel era-matched vintage commercials, makes station
bumps, and builds catalogs and a week of schedule.

```
git clone https://github.com/ProJabroni/fs42 fs42-setup
python3 fs42-setup/fs42_autofill.py
```

Update and re-run any time; it tops channels up and never downloads twice:

```
git -C fs42-setup pull && python3 fs42-setup/fs42_autofill.py
```

**Your own videos:** drop files into a channel's folder (subfolders are fine),
for example `catalog/classic_cartoons/cartoons/my_show/`, then re-run.
The Anime channel only turns on once you've added videos to `catalog/anime/shows/`.

## Your Google Drive videos

Plug an external drive into the Pi, then:

```
git -C fs42-setup pull && python3 fs42-setup/fs42_gdrive.py
```

It signs in to Google (read-only), lists your Drive folders, lets you pick which
folders go on which channel, copies just the video files to the external drive,
and links them into the channels. Run it again later to grab new episodes.
