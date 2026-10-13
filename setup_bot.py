#!/usr/bin/env python3
"""One-time setup: connect your Telegram bot, put this folder on GitHub as a
private repository, save the bot's details there as secrets, and start the
first check.

Before running it:
  1. In Telegram, message @BotFather, send /newbot and copy the token it gives you.
  2. Install the GitHub CLI and log in:  brew install gh  then  gh auth login

Then run:  python3 setup_bot.py
Running it again is safe: finished steps are skipped or simply redone.
"""

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time

from vhs_watch import TelegramError, send_message, telegram

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_NAME = "vhs-sprachtest-bot"
WORKFLOW = "check.yml"
TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
WELCOME = (
    "✅ <b>Connected!</b>\n"
    "I'll message you here when a <b>Sprachtest zur Einbürgerung</b> date at the Berlin VHS "
    "has free places. The list is checked every hour; no message means everything is still full."
)


def run(*cmd, input=None, check=True):
    return subprocess.run(cmd, cwd=HERE, input=input, text=True, capture_output=True, check=check)


def check_gh():
    if not shutil.which("gh"):
        sys.exit("The GitHub CLI isn't installed. Install it with `brew install gh`, run `gh auth login`, then rerun this.")
    if run("gh", "auth", "status", check=False).returncode != 0:
        sys.exit("The GitHub CLI isn't logged in. Run `gh auth login`, then rerun this.")


def wait_for_chat(token, minutes=5):
    """Returns the private chat of the first message someone sends the bot."""
    deadline = time.time() + minutes * 60
    offset = None
    while time.time() < deadline:
        payload = {"timeout": 25, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        for update in telegram(token, "getUpdates", payload, timeout=35):
            offset = update["update_id"] + 1
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                telegram(token, "getUpdates", {"offset": offset, "timeout": 0})  # mark it as read
                return chat
    sys.exit(f"No message arrived within {minutes} minutes. Send your bot a message in Telegram, then rerun this.")


def ask_token(tries=3):
    """Asks for the bot token until Telegram accepts one; returns (token, bot)."""
    for _ in range(tries):
        pasted = getpass.getpass("Paste the bot token from @BotFather and press Enter (nothing shows while you paste): ")
        # Also accept BotFather's whole message pasted in; the token is the 123456789:AA... part.
        m = TOKEN_PATTERN.search(pasted)
        if not pasted.strip():
            print("Nothing was pasted. In Telegram, copy the token from @BotFather's message, then paste it here.\n")
        elif not m:
            print(f"That doesn't look like a bot token (got {len(pasted.strip())} characters; "
                  "a token looks like 123456789:AAH...). Copy it again and paste it here.\n")
        else:
            try:
                return m.group(0), telegram(m.group(0), "getMe")
            except TelegramError as e:
                print(f"Telegram didn't accept that token ({e}). Copy it again from @BotFather and paste it here.\n")
    sys.exit("No working token yet. Rerun this when you have the token from @BotFather.")


def connect_telegram():
    token, bot = ask_token()
    print(f"\nFound your bot @{bot['username']}.")
    print(f"Open https://t.me/{bot['username']} in Telegram and tap Start (or send it any message).")
    print("Waiting for your message…")
    chat = wait_for_chat(token)
    send_message(token, chat["id"], WELCOME)
    print(f"Got it, {chat.get('first_name') or 'thanks'}. I sent you a test message in Telegram.\n")
    return token, chat["id"]


def publish_repo():
    if run("git", "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        if not run("git", "config", "user.email", check=False).stdout.strip():
            # Commit under your GitHub account's private no-reply address.
            user = json.loads(run("gh", "api", "user").stdout)
            run("git", "config", "user.name", user.get("name") or user["login"])
            run("git", "config", "user.email", f"{user['id']}+{user['login']}@users.noreply.github.com")
        run("git", "add", "-A")
        run("git", "commit", "-m",
            "Add VHS Sprachtest watcher\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    if run("git", "remote", "get-url", "origin", check=False).returncode != 0:
        print(f"Creating the private GitHub repository {REPO_NAME}…")
        result = run("gh", "repo", "create", REPO_NAME, "--private", "--source", HERE, "--remote", "origin", check=False)
        if result.returncode != 0:
            sys.exit(f"Couldn't create the repository:\n{result.stderr.strip()}")
    result = subprocess.run(["git", "push", "-u", "origin", "main"], cwd=HERE, text=True, capture_output=True,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if result.returncode != 0:
        hint = "Run `gh auth setup-git`, then rerun this."
        if "workflow" in result.stderr:
            hint = "Run `gh auth refresh -h github.com -s workflow`, then rerun this."
        sys.exit(f"Couldn't upload the files to GitHub:\n{result.stderr.strip()}\n{hint}")
    print("Uploaded the bot to GitHub.")


def store_secrets(token, chat_id):
    for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", str(chat_id))):
        result = run("gh", "secret", "set", name, input=value, check=False)
        if result.returncode != 0:
            sys.exit(f"Couldn't save the {name} secret:\n{result.stderr.strip()}")
    print("Saved the bot token and chat ID as GitHub secrets.")


def start_first_check():
    # A freshly pushed workflow can take a few seconds to show up.
    for _ in range(10):
        if run("gh", "workflow", "run", WORKFLOW, check=False).returncode == 0:
            print("Started the first check.")
            return
        time.sleep(3)
    print("Couldn't start the first check right away; it will run at the next hour anyway.")


def main():
    check_gh()
    try:
        token, chat_id = connect_telegram()
    except TelegramError as e:
        sys.exit(f"Telegram error: {e}")
    publish_repo()
    store_secrets(token, chat_id)
    start_first_check()
    url = run("gh", "repo", "view", "--json", "url", "--jq", ".url", check=False).stdout.strip()
    print(f"\nAll set. The bot checks every hour and only messages you when a place is free.\nRepository: {url}")


if __name__ == "__main__":
    main()
