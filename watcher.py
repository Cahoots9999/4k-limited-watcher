```yaml
name: 4K Limited Edition Watcher

on:
  workflow_dispatch:

  schedule:
    # Twice per hour, deliberately not on the hour.
    # GitHub recommends avoiding busy periods such as the
    # beginning of an hour.
    - cron: "17,47 * * * *"

permissions:
  contents: write
  pages: write
  id-token: write

concurrency:
  group: "4k-limited-watcher"
  cancel-in-progress: false


jobs:
  watch-and-publish:

    runs-on: ubuntu-latest

    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}

    steps:

      # -------------------------------------------------------
      # Get repository
      # -------------------------------------------------------

      - name: Check out repository
        uses: actions/checkout@v4


      # -------------------------------------------------------
      # Python
      # -------------------------------------------------------

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"


      # -------------------------------------------------------
      # Dependencies
      # -------------------------------------------------------

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt


      # -------------------------------------------------------
      # Run watcher
      # -------------------------------------------------------

      - name: Check Ginza and iMusic
        run: |
          python watcher.py


      # -------------------------------------------------------
      # Save database + RSS to repository
      # -------------------------------------------------------

      - name: Save watcher state
        run: |

          git config user.name "4K Watcher"
          git config user.email \
            "41898282+github-actions[bot]@users.noreply.github.com"

          git add data/products.json
          git add public/4k-limited.xml

          if git diff --cached --quiet; then
            echo "No changes to commit."
          else
            git commit -m "Update 4K limited edition feed"
            git push
          fi


      # -------------------------------------------------------
      # GitHub Pages
      # -------------------------------------------------------

      - name: Configure GitHub Pages
        uses: actions/configure-pages@v5


      - name: Upload RSS
        uses: actions/upload-pages-artifact@v3
        with:
          path: ./public


      - name: Deploy RSS feed
        id: deployment
        uses: actions/deploy-pages@v4
```
