import os
from flask import Flask, render_template, request, jsonify, redirect, url_for
from bible_data import BibleData

app = Flask(__name__, static_folder='static', template_folder='templates')

# Resolve absolute path to data file
base_dir = os.path.dirname(os.path.abspath(__file__))
data_path = os.path.join(base_dir, 'data', 'kjv_source.json')

bible = BibleData(data_path)

@app.route('/')
def index():
    books = bible.get_all_books()
    return render_template('index.html', books=books)

@app.route('/book/<identifier>')
def book_view(identifier):
    book = bible.get_book(identifier)
    if not book:
        return "Book not found", 404
    return render_template('book.html', book=book)

@app.route('/book/<identifier>/chapter/<int:chapter_num>')
def chapter_view(identifier, chapter_num):
    book = bible.get_book(identifier)
    if not book:
        return "Book not found", 404

    chapter = bible.get_chapter(identifier, chapter_num)
    if chapter is None:
        return "Chapter not found", 404

    num_chapters = len(book.get('chapters', []))

    return render_template(
        'chapter.html',
        book=book,
        chapter=chapter,
        chapter_num=chapter_num,
        num_chapters=num_chapters
    )

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    results = []
    if query:
        results = bible.search(query)
    return render_template('search.html', query=query, results=results)

# --- API Endpoints ---

@app.route('/api/book/<identifier>')
def api_book(identifier):
    book = bible.get_book(identifier)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    return jsonify({
        'name': book['name'],
        'abbrev': book['abbrev'],
        'chapters_count': len(book.get('chapters', []))
    })

@app.route('/api/chapter/<identifier>/<int:chapter_num>')
def api_chapter(identifier, chapter_num):
    book = bible.get_book(identifier)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    chapter = bible.get_chapter(identifier, chapter_num)
    if chapter is None:
        return jsonify({'error': 'Chapter not found'}), 404

    return jsonify({
        'book': book['name'],
        'chapter': chapter_num,
        'verses': chapter
    })

@app.route('/api/verse/<identifier>/<int:chapter_num>/<int:verse_num>')
def api_verse(identifier, chapter_num, verse_num):
    book = bible.get_book(identifier)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    verse = bible.get_verse(identifier, chapter_num, verse_num)
    if verse is None:
        return jsonify({'error': 'Verse not found'}), 404

    return jsonify({
        'book': book['name'],
        'chapter': chapter_num,
        'verse': verse_num,
        'text': verse
    })

@app.route('/api/search')
def api_search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Missing query parameter "q"'}), 400
    results = bible.search(query)
    return jsonify({
        'query': query,
        'results_count': len(results),
        'results': results
    })

if __name__ == '__main__':
    app.run(debug=True, port=5001)
