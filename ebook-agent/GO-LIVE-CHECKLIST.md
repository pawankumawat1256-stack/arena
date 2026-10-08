# Go-Live Checklist (for non-developers)

This guide takes you from "the code is on GitHub" to "the agent runs on a server with HTTPS and can receive Gumroad and Stripe sales." Work from top to bottom. Each step says what you should see when it worked. The first run takes about 2 to 3 hours, mostly waiting for keys and DNS.

**Keep every key secret.** Never paste a key into chat, email, a screenshot, or GitHub. Keys go only into the `.env` file on the server. `.env` is already excluded from Git.

---

## Part 1: What you need before you start

- [ ] An OpenAI account with billing set up (the agent calls OpenAI models).
- [ ] A Gumroad account (the PDF is sold here).
- [ ] An Instagram Business or Creator account, linked to a Facebook Page you manage.
- [ ] A Google account for Gmail and Google Drive. Use the account that will send the emails.
- [ ] A Notion account.
- [ ] A Stripe account (optional. Skip Part 2F if you do not use Stripe).
- [ ] A domain name, such as `yourbrand.com`, from any registrar.
- [ ] A small Linux server (VPS) running **Ubuntu 24.04 LTS** with at least **2 GB RAM**. Any provider works.
- [ ] **Python 3.11 or newer on your own computer.** You need it once, for the Google sign-in. Download it from python.org.

---

## Part 2: Get your keys

Open a notepad. Paste each key as you get it. You will copy them into `.env` in Part 4.

### 2A. OpenAI (`OPENAI_API_KEY`)
1. Go to platform.openai.com and sign in.
2. Open **Settings → Billing**. Add a payment method and add credits.
3. Go to **platform.openai.com/api-keys**, click **Create new secret key**, and copy it. It starts with `sk-`. You cannot see it again later.

### 2B. Gumroad (`GUMROAD_ACCESS_TOKEN`)
1. Open **gumroad.com/settings/advanced#application-form**.
2. Create an application. Name: `Ebook Agent`. Redirect URL: `https://api.yourdomain.com/health` (replace `yourdomain.com` with your domain). This app does not use the login redirect, so any valid https address works.
3. On the application page, click **Generate access token** and copy it. If you are asked to choose scopes, tick **view_sales**.

### 2C. Instagram and Meta (`INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`)
Meta changes its menus often. If a name here looks different, look for the same setting. Your goal is a **Page access token** and your **Instagram account ID**.

1. In the Instagram app, go to **Settings and privacy → Account type and tools → Switch to professional account**. Choose Business or Creator.
2. Create a Facebook Page if you do not have one (facebook.com/pages/create). Link the Instagram account to the Page (Page settings → Linked accounts → Instagram).
3. Go to **developers.facebook.com → My Apps → Create App**. Choose the Business app type. Add the **Facebook Login** product (Meta may call it Facebook Login for Business).
4. Keep the app in **Development mode**. It works for accounts where you have a role in the app. Meta requires App Review only if other people's accounts must connect.
5. Open **developers.facebook.com/tools/explorer**. Select your app. Click **Generate Access Token** and tick these permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`. (`pages_show_list` is needed to see your Page. It is not on the original permission list, so it is added here.)
6. In the Explorer, type `me/accounts` and click **Submit**. Find your Page and copy its `id`.
7. Make the token last longer. In your browser, open this address, replacing the four capitalised parts. The App ID and App Secret are on developers.facebook.com → your app → Settings → Basic. The short token is the one from step 5:
   `https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN`
   Copy the `access_token` from the result. Paste it into the Explorer and run `me/accounts` again. Copy the **Page** `access_token` from that result. This is the value for `INSTAGRAM_ACCESS_TOKEN`. A Page token made from a long-lived user token does not expire on a timer. Meta can still revoke it, for example if you change your Facebook password or remove the app.
8. In the Explorer, run `YOUR_PAGE_ID?fields=instagram_business_account`. The result contains `instagram_business_account` with an `id`. That is `INSTAGRAM_USER_ID`.

### 2D. Google: Gmail and Drive
This creates two files: `google_client_secret.json` and, later, `google_token.json`.

1. Go to **console.cloud.google.com**. Create a project named `Ebook Agent` and make sure it is selected at the top.
2. Open **APIs & Services → Library**. Search for **Gmail API** and click **Enable**. Search for **Google Drive API** and click **Enable**.
3. Open **APIs & Services → OAuth consent screen**. Google now calls this **Google Auth Platform**.
   - Fill in the app name (`Ebook Agent`), your support email, and your developer contact email.
   - Under **Audience**, choose **External**.
   - Click **Publish app** and confirm **In production**. This matters: while the app stays in "Testing", Google expires the sign-in every 7 days and the emails stop. If Google asks you to submit the app for verification, stop and tell me before you do anything else.
   - Under **Data access**, add the scopes `gmail.compose` and `drive.file`.
4. Open **Clients → + Create client**. Application type: **Desktop app**. Click **Create**, then **Download JSON**. Rename the file to `google_client_secret.json`.

### 2E. Notion (`NOTION_API_KEY`, `NOTION_PARENT_PAGE_ID`)
1. Open **notion.so/developers/tokens** (Personal access tokens in the Notion developer portal).
2. Click **New token**. Name: `Ebook Agent`. Choose the **Notion API** capability. Click **Create token** and copy it. It starts with `ntn_`.
   If you do not see **New token**, a workspace owner on a Business or Enterprise plan must allow it under **Settings → Connections**.
3. In Notion, create a page named `Ebook Manuscripts` in the same workspace as the token. Open it. Copy the 32-character code at the end of the page address, after the last dash and before any `?`. That is the page ID.

### 2F. Stripe (optional: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`)
1. Go to **dashboard.stripe.com**. Turn on **Test mode** (the switch at the top). Use test mode first. It uses fake money.
2. Open **Developers → API keys**. Click **Reveal** next to the secret key and copy it. It starts with `sk_test_`.
3. The webhook secret comes in Part 6, after the server is live.

### 2G. Your business details
- Your physical postal address. Marketing email is blocked until it is set, which is required by email law. This is `BUSINESS_POSTAL_ADDRESS`.
- A dashboard password you choose. This is `DASHBOARD_PASSWORD`.

---

## Part 3: Set up the server

### 3A. Create the server and point your domain
1. Create an Ubuntu 24.04 LTS server with 2 GB RAM or more. Add your SSH key (or use the password your provider gives you). Write down its public IP address.
2. In your domain registrar's DNS settings, add two **A records**: `api` pointing to your server IP, and `dashboard` pointing to your server IP.
3. Wait until DNS works. This can take minutes to a few hours. On your computer, run `nslookup api.yourdomain.com`. You should see your server IP.

### 3B. Connect to the server
On your computer, open Terminal (macOS or Linux) or PowerShell (Windows):
```
ssh root@YOUR_SERVER_IP
```
If your provider gave you a different username, use that instead of `root`.

### 3C. Firewall (copy and paste)
```
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```
Expected: `Firewall is active and enabled on system startup`.

### 3D. Install Docker (copy and paste)
```
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker run hello-world
```
Expected: a message that starts with `Hello from Docker!`.

### 3E. Install Caddy, which provides HTTPS (copy and paste)
```
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```
Expected: `caddy version` prints a version number.

---

## Part 4: Put the code on the server and set it up

1. Install Git and download the code:
   ```
   sudo apt install -y git
   git clone -b arena/485bbc42-arena https://github.com/pawankumawat1256-stack/arena.git
   cd arena/ebook-agent
   ```
   After the pull request is merged into `main`, you can drop `-b arena/485bbc42-arena`.

2. Create your settings file and the two folders the app uses:
   ```
   cp .env.example .env
   mkdir -p data secrets
   sudo chown -R 1000:1000 data secrets
   ```
   The `chown` line matters. The app runs as user ID 1000 inside Docker and must be able to write to these folders.

3. Create three secret codes. Run this command three times and save each result in your notepad:
   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   You will use them for `APP_SECRET_KEY`, `API_KEY`, and `GUMROAD_WEBHOOK_TOKEN`.

4. Open the settings file:
   ```
   nano .env
   ```
   Fill in these lines. Keep `APP_ENV=production` as it is.
   - `APP_SECRET_KEY=` (first secret code)
   - `API_KEY=` (second secret code)
   - `PUBLIC_BASE_URL=https://api.yourdomain.com`
   - `DASHBOARD_PASSWORD=` (your dashboard password)
   - `OPENAI_API_KEY=`
   - `NOTION_API_KEY=`
   - `NOTION_PARENT_PAGE_ID=`
   - `BUSINESS_POSTAL_ADDRESS=`
   - `GUMROAD_ACCESS_TOKEN=`
   - `GUMROAD_WEBHOOK_TOKEN=` (third secret code)
   - `INSTAGRAM_ACCESS_TOKEN=`
   - `INSTAGRAM_USER_ID=`
   - `STRIPE_SECRET_KEY=` (only if you use Stripe)
   - `GOOGLE_DRIVE_FOLDER_ID=` (optional)

   Save and exit: press **Ctrl+O**, **Enter**, then **Ctrl+X**.

5. Start the app:
   ```
   sudo docker compose up -d --build
   sudo docker compose ps
   ```
   The first build takes a few minutes. Expected: three services (`api`, `scheduler`, `dashboard`) show as running.

6. Check the app on the server itself:
   ```
   curl -s http://127.0.0.1:8000/health
   ```
   Expected: `{"status":"ok", ...}`. If you see `Database unavailable`, run `sudo docker compose logs --tail=50 api` and send me the output with every key removed.

---

## Part 5: Turn on HTTPS

1. Open the Caddy settings file:
   ```
   sudo nano /etc/caddy/Caddyfile
   ```
   Delete everything in the file. Paste the following, replacing `yourdomain.com` with your domain:
   ```
   api.yourdomain.com {
       reverse_proxy 127.0.0.1:8000
   }

   dashboard.yourdomain.com {
       reverse_proxy 127.0.0.1:8501
   }
   ```
   Save and exit (Ctrl+O, Enter, Ctrl+X).

2. Check the file and load it:
   ```
   sudo caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```
   Expected: `Valid configuration` and no errors. Caddy gets the HTTPS certificates automatically once DNS from Part 3A works.

3. In your computer's browser, check both addresses:
   - `https://api.yourdomain.com/health` shows `"status": "ok"`.
   - `https://dashboard.yourdomain.com` asks for your dashboard password.

---

## Part 6: Connect Gumroad (and Stripe) to the server

1. **Register the Gumroad webhook.** This tells Gumroad to send sale events to your server:
   ```
   cd ~/arena/ebook-agent
   sudo docker compose exec api python -m integrations.gumroad register
   sudo docker compose exec api python -m integrations.gumroad list
   ```
   Expected: one line per Gumroad event type with no error. The `list` command shows lines with `post_url`.

2. **Create your product record** so that a Gumroad sale unlocks the right file.
   - If you have no ebook record yet, create one first with `POST /ebooks` (topic and audience).
   - Create the product. Replace the capitalised parts:
     ```
     curl -s -X POST https://api.yourdomain.com/products \
       -H "X-API-Key: YOUR_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"ebook_id": 1, "tier": "basic", "name": "YOUR PRODUCT NAME", "price_usd": YOUR_PRICE, "platform": "gumroad", "permalink": "YOUR_GUMROAD_CODE", "file_path": "/app/data/ebooks/YOUR_FILE.pdf"}'
     ```
   - `ebook_id`: the number of your ebook (shown on the dashboard's Ebooks page).
   - `tier`: `basic`, `pro`, `bundle`, or `custom`.
   - `price_usd`: the price you set on Gumroad, for example `9.99`.
   - `permalink`: the code after `gumroad.com/l/` in your product link.
   - `file_path`: copy the PDF into `~/arena/ebook-agent/data/ebooks/` on the server. The app can deliver only files stored inside `data/`.

3. **Stripe (only if you use it).** In Stripe test mode, go to **Developers → Webhooks → Add endpoint**.
   - Endpoint URL: `https://api.yourdomain.com/webhook/stripe`
   - Events: `checkout.session.completed` and `charge.refunded`
   - Click **Add endpoint**. Open it, click **Reveal** under **Signing secret**, and copy the value (it starts with `whsec_`).
   - On the server, run `nano .env`, set `STRIPE_WEBHOOK_SECRET=`, save, then run `sudo docker compose up -d`.
   Stripe's menu names can shift slightly. The goal is the same: an endpoint and its signing secret.

4. **Google sign-in.** Do this on **your computer**, not the server. You need `google_client_secret.json` from Part 2D.
   - Put `google_client_secret.json` in a folder named `secrets` inside the project on your computer. To get the project, download the repository from GitHub as a ZIP, or run `git clone -b arena/485bbc42-arena https://github.com/pawankumawat1256-stack/arena.git`.
   - Open Terminal (macOS or Linux) in the `ebook-agent` folder, then run:
     ```
     python3 -m venv .venv
     source .venv/bin/activate
     pip install -r requirements.txt
     python -m integrations.google_auth
     ```
     Windows PowerShell instead: `py -3.11 -m venv .venv`, then `.venv\Scripts\Activate.ps1`, then the same `pip` and `python` lines.
   - A browser opens. Sign in with the Google account that will send the emails. You will see "Google hasn't verified this app". Click **Advanced → Go to Ebook Agent → Continue**. This is expected for an app only you use. The file `secrets/google_token.json` is created.
   - Copy both files to the server. Run this on your computer:
     ```
     scp secrets/google_client_secret.json secrets/google_token.json root@YOUR_SERVER_IP:~/arena/ebook-agent/secrets/
     ```
   - On the server, run:
     ```
     cd ~/arena/ebook-agent
     sudo chown -R 1000:1000 secrets
     sudo docker compose up -d
     ```

---

## Part 7: Check that the agent works

Run these one at a time. Each step says what you should see.

1. **Health.** `curl -s https://api.yourdomain.com/health` → `"status": "ok"`.
2. **Dashboard.** Open `https://dashboard.yourdomain.com` and enter your dashboard password. The Overview page loads without an error.
3. **Scheduler.** `sudo docker compose logs --tail=30 scheduler` → a line that says `Scheduler starting in Asia/Kolkata with 7 jobs`.
4. **AI test.** This uses a small amount of OpenAI credit.
   ```
   curl -s -X POST https://api.yourdomain.com/research \
     -H "X-API-Key: YOUR_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"topic": "AI tools for solopreneurs", "audience": "freelancers and small business owners"}'
   ```
   Expected: a reply with `"status": "queued"`, a `run_id`, and a `poll` address such as `/runs/5`.
   Check the run (replace `5` with your run number):
   ```
   curl -s https://api.yourdomain.com/runs/5 -H "X-API-Key: YOUR_API_KEY"
   ```
   The status moves from `queued` to `running` to `succeeded`. This usually takes a few minutes. If it says `failed`, the reply shows the error. Check the matching key in `.env` and send me the error text with every key removed.
5. **Gumroad link.** `sudo docker compose exec api python -m integrations.gumroad list` shows your webhook address.
6. **Logs.** `sudo docker compose logs --tail=50 api` shows no red error lines after your tests.

---

## Part 8: Everyday commands

- **See what the app is doing:** `sudo docker compose logs -f --tail=100 api` (press Ctrl+C to stop).
- **After changing `.env`:** `sudo docker compose up -d`
- **Update to the newest code:** `cd ~/arena/ebook-agent && git pull && sudo docker compose up -d --build`
- **Back up your data, weekly:** `tar czf ~/ebook-backup-$(date +%F).tar.gz data`. Then copy the file to a safe place off the server. Keep `.env` and `secrets/` in a password manager, not next to the backups.

---

## Part 9: Limits to know before you rely on it

- **Instagram needs a public image address.** The agent does not host images. Upload each JPEG to a public place, such as your website, and give its address to the post.
- **Gumroad events are triggers.** The agent re-reads each sale from Gumroad and reconciles every 6 hours, so a missed event is caught.
- **Keep Stripe in test mode** until you have finished test purchases and a test refund.
- **Do not share keys.** If a key leaks, revoke it at its provider and replace it in `.env`.
- **If something breaks,** send me the command you ran and its output. Remove every key first.
