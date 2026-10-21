# Tournament Manager

A web app where a Tournament Coordinator / Admin creates tournaments. Each tournament gets its own **registration page link**: players open it, type their **IGN** and they're in. No account needed.

## Put it online (Render + Neon, both free)

Render hosts the app. Neon holds the database: Render's own free database is deleted after 30 days, Neon's isn't.

**1. Create the database (Neon)**
1. Sign up at https://neon.tech (you can sign in with GitHub) and create a project, e.g. `tournament`.
2. On the project dashboard click **Connect** and copy the connection string. It looks like
   `postgresql://user:password@ep-xxxx.region.aws.neon.tech/neondb?sslmode=require`

**2. Deploy the app (Render)**
1. Sign up at https://render.com with your GitHub account.
2. Click **New** → **Blueprint**, and pick the `tournament-manager` repository (give Render access to it if asked).
3. When it asks for `DATABASE_URL`, paste the Neon connection string. Click **Deploy Blueprint**.
4. After a few minutes your app is live at an address like `https://tournament-manager-xxxx.onrender.com`.
5. Open it and **create the Admin account first** (the setup page only works once).

Every push to GitHub redeploys the app automatically. Your data stays in Neon.

Good to know about the free plan: the app goes to sleep after 15 minutes with no visitors, and the first visit after that takes about a minute to load. Open the site a few minutes before an event starts.

## Run it on your own PC (Windows)

1. Install Python 3.10+ from python.org (tick "Add Python to PATH").
2. Double-click `run.bat`. The first run installs Flask automatically.
3. Open http://localhost:5000
4. The first time, you'll create the **Admin account**. Extra Coordinators are added on the *Coordinators* page.

Players on the same network open `http://<this-PC's-IP>:5000` on their phone or PC.
(If they can't reach it, allow Python through Windows Firewall when prompted.)

## Forgot the Coordinator / Admin login?

1. In Render, open your service, then **Environment**, then **Add Environment Variable**: key `ADMIN_RESET_PASSWORD`, value = your new password. Click **Save** (Render restarts the app).
2. Open **Logs**: a line `ADMIN PASSWORD RESET. Admin usernames: ...` shows your username(s).
3. Log in with that username and the new password.
4. **Delete the `ADMIN_RESET_PASSWORD` variable** again and save.

On your own PC: run `set ADMIN_RESET_PASSWORD=yournewpassword` and then `run.bat` in the same Command Prompt window.

## Tournament options (set when creating)

| Setting | Choices |
|---|---|
| Swiss format | Best of 1, or Best of 3 |
| Swiss rounds | A fixed number, or blank for automatic (based on player count) |
| Top Cut | None, Top 2, 4, 8, 16, 32. The Top Cut is **always Best of 3**, single elimination |
| Player cap | Optional |

## How an event runs

1. **Create the tournament.** The app makes a registration page for it and shows its link (with a **Copy link** button and a QR code) on the tournament page.
2. **Share the link.** Players open it, type their IGN and tap *Register*. If that IGN is already registered in the tournament, they're told to pick another. Their phone remembers them, so they don't need an account or password.
   Walk-ins without a phone: add them by IGN on the Players tab.
3. **Start tournament**: Round 1 is paired at random.
4. Each round, a player reports the result on their phone and the opponent taps **Confirm** (or **Dispute**). The Coordinator can enter or override any result, e.g. for walk-ins or a player who switched phones.
5. When every match has a result, click **Pair next round**. Players are paired with others on the same points, with no rematches. With an odd number of players, the lowest-ranked player who hasn't had a bye gets one (counts as a win).
6. After the last Swiss round: **End Swiss · build Top X** seeds the bracket from standings (1v8, 4v5, 2v7, 3v6), or **Finish** if there's no cut.
7. Bracket rounds advance until the Finals, then the champion is crowned.

Other tools: players can **Drop**, and the Coordinator can drop or reinstate players. **Re-pair round** redoes the current pairings if no results are in yet. Tournament **Settings** can be edited until the event starts.

Anyone can view tournaments, pairings and standings without logging in. Only Coordinators log in (*Coordinator login* at the top).

## Standings

Win = 3 pts, Draw = 1 pt. Ties are broken by **OMW%**, then **GW%**, then **OGW%**, with a 33% minimum per opponent. Byes count as a 2-0 win (1-0 in Bo1) but are left out of opponents' percentages.

## Files

- `app.py`: web app (routes, database)
- `engine.py`: pairing, standings and bracket logic
- `templates/`, `static/`: pages and styling
- `render.yaml`: Render deployment settings
- On your PC, data is saved in `instance/tournament.db` (SQLite). Online, it's saved in the Postgres database set by `DATABASE_URL`.
