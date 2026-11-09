# Colchester School Calendar

A free calendar website for Jack Jackter Intermediate and William J. Johnston Middle School events.

* **Sources:** the Colchester Public Schools and WJJMS public Google calendars, the same calendars ParentSquare displays. No login is needed.
* **Updates:** GitHub Actions runs `build.py` every morning (about 6 AM Eastern) and republishes the site on GitHub Pages. Run it by hand from the **Actions** tab: *Update calendar* then *Run workflow*.
* **Phone calendar:** subscribe to `calendar.ics` on the site to see the same events in Apple or Google Calendar.
* **Optional:** add a repository secret `PS_CALENDAR_TOKEN` (the token part of a ParentSquare "Subscribe" calendar link) to also pull ParentSquare-only events.
* **Change schools or calendars:** edit `config.json`.
