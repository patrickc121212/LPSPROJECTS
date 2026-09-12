# Biblbot Plan

A simple web application and API to browse and search the King James Version (KJV) of the Bible.

## Features

1.  **Bible Browser**
    -   Home page listing all 66 books.
    -   Book view showing chapters.
    -   Chapter view showing all verses in that chapter.
2.  **Search**
    -   Keyword and phrase search across the entire Bible.
    -   Search results with direct links to the chapter/verse.
3.  **API**
    -   Retrieve a specific verse: `GET /api/verse/<book>/<chapter>/<verse>`
    -   Retrieve a whole chapter: `GET /api/chapter/<book>/<chapter>`
4.  **CLI Tool** (Future)
    -   Quick lookup from terminal.

## Architecture

-   **Backend:** Python 3 + Flask.
-   **Data Storage:** `kjv_source.json` (loaded into memory on startup).
-   **Frontend:** Jinja2 templates + basic CSS.
-   **Search:** Simple in-memory case-insensitive keyword matching.

## Directory Layout

-   `app.py`: Flask application and routing.
-   `bible_data.py`: Data handling class for loading and querying the JSON.
-   `static/style.css`: Minimalist styling.
-   `templates/`:
    -   `base.html`: Shared layout.
    -   `index.html`: List of books.
    -   `book.html`: List of chapters in a book.
    -   `chapter.html`: Verses in a chapter.
    -   `search.html`: Search results.

## Roadmap

-   [x] Initial Data exploration.
-   [ ] Create `bible_data.py` helper.
-   [ ] Create Flask `app.py`.
-   [ ] Design HTML templates.
-   [ ] Implement search functionality.
-   [ ] Add API endpoints.
