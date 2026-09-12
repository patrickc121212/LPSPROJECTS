import sys
import os

# Add the project directory to sys.path
sys.path.append(os.path.join(os.getcwd(), "LPsProjects/Biblbot"))
from bible_data import BibleData

data_path = "LPsProjects/Biblbot/data/kjv_source.json"
bible = BibleData(data_path)

print(f"Total books loaded: {len(bible.get_all_books())}")

# Test simple lookup
genesis_1_1 = bible.get_verse("gn", 1, 1)
print(f"Genesis 1:1 - {genesis_1_1}")

# Test search
search_query = "Jesus wept"
results = bible.search(search_query)
print(f"Search results for '{search_query}': {len(results)}")
for r in results:
    print(f"{r['book_name']} {r['chapter']}:{r['verse_num']} - {r['text']}")

# Test John 3:16
john_3_16 = bible.get_verse("John", 3, 16)
print(f"John 3:16 - {john_3_16}")
