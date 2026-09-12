import json
import os

class BibleData:
    def __init__(self, json_path):
        self.json_path = json_path
        self.books = []
        self.books_by_abbrev = {}
        self.books_by_name = {}
        self.load_data()

    def load_data(self):
        if not os.path.exists(self.json_path):
            raise FileNotFoundError(f"Data file not found: {self.json_path}")

        with open(self.json_path, 'r', encoding='utf-8-sig') as f:
            self.books = json.load(f)

        for book in self.books:
            self.books_by_abbrev[book['abbrev'].lower()] = book
            self.books_by_name[book['name'].lower()] = book

    def get_all_books(self):
        return self.books

    def get_book(self, identifier):
        """identifier can be name or abbreviation (case-insensitive)"""
        identifier = identifier.lower()
        return self.books_by_abbrev.get(identifier) or self.books_by_name.get(identifier)

    def get_chapter(self, book_id, chapter_num):
        book = self.get_book(book_id)
        if not book:
            return None

        chapters = book.get('chapters', [])
        if 1 <= chapter_num <= len(chapters):
            return chapters[chapter_num - 1]
        return None

    def get_verse(self, book_id, chapter_num, verse_num):
        chapter = self.get_chapter(book_id, chapter_num)
        if not chapter:
            return None

        if 1 <= verse_num <= len(chapter):
            return chapter[verse_num - 1]
        return None

    def search(self, query):
        """Simple keyword search across all verses"""
        results = []
        query = query.lower()
        for book in self.books:
            for c_idx, chapter in enumerate(book.get('chapters', [])):
                for v_idx, verse in enumerate(chapter):
                    if query in verse.lower():
                        results.append({
                            'book_name': book['name'],
                            'book_abbrev': book['abbrev'],
                            'chapter': c_idx + 1,
                            'verse_num': v_idx + 1,
                            'text': verse
                        })
        return results
