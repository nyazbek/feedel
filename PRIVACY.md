# Privacy policy

feedel is a personal feed reader that runs on your own computer. There is no feedel server and no feedel account.

**What it stores.** Everything feedel keeps (the sources you follow, what you have seen and saved, your notes, your messages, your settings and any access token you paste in) lives in a database file in the folder where you installed it. Nothing is sent to the author of feedel or to any third party.

**What it talks to.** feedel fetches the things you asked for from the sites that host them (YouTube, X through Nitter and X's public syndication endpoint, arXiv, Substack and other feeds, Zulip if you connected it) and, if you connected an Instagram account, reads and sends your direct messages through Meta's official Instagram API on your behalf. Those requests are made from your computer with your own credentials, and only when you use the corresponding feature.

**Instagram data.** With an Instagram token connected, feedel reads your conversations and the messages in them, shows them in its messages tab and stores them in its local database so they stay readable offline. It sends a message only when you write one. It never shares that data with anyone, and you can delete it at any time by disconnecting the account under the gear (which removes the token) and deleting the local database. To revoke feedel's access on Meta's side, remove the app under Instagram → Settings → Website permissions.

**Deletion.** Uninstalling feedel and deleting its folder removes everything it ever stored.

**Contact.** Questions go to the repository's issues at https://github.com/nyazbek/feedel/issues.
