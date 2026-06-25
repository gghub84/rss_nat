# Nature/PubMed RSS to Discord

Free GitHub Actions setup for posting PubMed RSS updates to a Discord channel.

## Setup

1. Create a Discord webhook for your target channel.
2. Create a GitHub repository and upload these files.
3. In GitHub, add a repository secret named `DISCORD_WEBHOOK_URL`.
4. Create a PubMed RSS feed for your search and paste it into `config.json`.
5. Make sure the PubMed RSS URL uses `limit=50` or `limit=100`.
6. Run the workflow manually once from the Actions tab.

The first run seeds existing items and posts one setup message. Future runs post new matching entries.
