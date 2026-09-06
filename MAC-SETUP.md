# 🖥️ My Mac Setup Adventure!

A step-by-step guide to get your Mac ready for coding — do these steps IN ORDER, one at a time. Don't skip ahead! 🚀

---

## 🧭 Step 0: Open the Terminal

The Terminal is a special app where you type commands instead of clicking buttons. Here's how to find it:

1. Look at the top-right corner of your screen for the little magnifying glass 🔍 (it's called **Spotlight**), or press `⌘ Command + Space` at the same time.
2. Type the word `Terminal`
3. Press `Enter` (a black or white window will pop up — that's your Terminal!)

You'll type commands into this window and press `Enter` after each one. That's it — you've got this! 💪

---

## 1️⃣ Step 1: Install Xcode Command Line Tools

This is a toolbox that gives your Mac important tools like `git` and `make`. You need this FIRST before anything else.

**Type this in Terminal and press Enter:**
```
xcode-select --install
```

- A little window will pop up asking if you want to install some software.
- Click **Install**.
- Then click **Agree** to the license.
- ⏳ This can take a few minutes (sometimes up to 15-20 mins). Just wait — don't close the window!

**How do you know it worked?** Type this and press Enter:
```
git --version
```
If you see something like `git version 2.xx.x`, you did it! 🎉

---

## 2️⃣ Step 2: Install Homebrew (this is the "brew" tool)

Homebrew is like an App Store, but for the Terminal — it helps you install lots of cool programs easily (like Python!).

**Copy and paste this WHOLE line into Terminal, then press Enter:**
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

- It might ask for your Mac's password (the one you use to log in). Type it and press Enter — **you won't see the letters appear, that's normal, just keep typing and press Enter.**
- Wait for it to finish. You'll see a lot of text scroll by — that's normal!

### ⚠️ Important extra step (don't skip this!)

At the very end, Homebrew will show a message called **"Next steps"** with 1-2 commands starting with `echo`. You need to copy and run those too! They look something like this (but copy YOUR OWN from your screen, since it can vary a little):

```
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> /Users/YOURNAME/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

This tells your Mac "hey, brew lives here!" so it can find it next time.

**How do you know it worked?** Close the Terminal window completely, open a fresh new one (Step 0), and type:
```
brew --version
```
If you see something like `Homebrew 4.x.x`, hooray — brew is installed! 🍺✨

---

## 3️⃣ Step 3: Install Python (using brew)

Now that brew is working, getting Python is easy!

**Type this and press Enter:**
```
brew install python
```

Wait for it to finish downloading and installing.

**How do you know it worked?** Type:
```
python3 --version
```
If you see something like `Python 3.xx.x`, you're all set! 🐍

---

## ✅ Quick Recap (the correct order!)

| Step | What you install | Command |
|------|------------------|---------|
| 1 | Xcode Command Line Tools (gives you `git` + `make`) | `xcode-select --install` |
| 2 | Homebrew (`brew`) | the long `curl` command above |
| 2b | Tell your Mac where brew lives | the `echo` command from "Next steps" |
| 3 | Python | `brew install python` |

---

## 🆘 Troubleshooting Tips

- **"command not found: brew"** — You skipped the `echo` step in Step 2, or you need to close and reopen Terminal.
- **It asks for a password and nothing shows on screen** — That's totally normal! Just type carefully and press Enter.
- **Something looks stuck** — Give it a minute. Big installs (like Step 1) can take a while.
- **Still stuck?** Copy the exact red/error text you see and ask an adult (or Claude!) to help — the error message usually tells us exactly what's wrong.

You're doing real programmer stuff now. Nice work! 🎮👨‍💻

---

Once this is done, head back to the [README](README.md#quickstart) and pick up at `git clone` — you're ready.
