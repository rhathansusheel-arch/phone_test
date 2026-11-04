"""Hard rule 3: no trademarks, characters or brands anywhere. Also banned listing words."""

from __future__ import annotations

import re

from . import settings

# Brands and characters that must never appear. Add more any time (lowercase).
TRADEMARKS = [
    "disney", "pixar", "marvel", "dc comics", "bluey", "bingo heeler", "pokemon", "pokémon", "pikachu",
    "crayola", "lego", "barbie", "hot wheels", "matchbox", "tonka", "paw patrol", "peppa pig", "peppa",
    "frozen", "elsa", "olaf", "mickey", "minnie", "mickey mouse", "spider-man", "spiderman", "batman",
    "superman", "star wars", "harry potter", "hogwarts", "hello kitty", "sanrio", "kuromi", "cinnamoroll",
    "sesame street", "elmo", "cocomelon", "minecraft", "roblox", "fortnite", "nintendo", "mario",
    "super mario", "zelda", "sonic", "transformers", "thomas the tank", "thomas & friends", "my little pony",
    "care bears", "smurfs", "sponge bob", "spongebob", "teenage mutant", "ninja turtles", "scooby",
    "looney tunes", "bugs bunny", "garfield", "snoopy", "peanuts", "winnie the pooh", "pooh",
    "dr. seuss", "dr seuss", "grinch", "the grinch", "rudolph", "frosty the snowman", "paddington",
    "peter rabbit", "moana", "encanto", "lilo", "stitch", "toy story", "cars movie", "lightning mcqueen",
    "jurassic park", "jurassic world", "godzilla", "hulk", "avengers", "pj masks", "blippi",
    "baby shark", "pinkfong", "gabby's dollhouse", "squishmallows", "stanley cup", "jeep", "ford",
    "tesla", "lamborghini", "ferrari", "porsche", "john deere", "caterpillar", "cat excavator",
    "kenworth", "peterbilt", "harley", "boeing", "airbus", "nasa", "coca-cola", "pepsi", "mcdonald",
    "nike", "adidas", "barney", "teletubbies", "pj mask", "dora", "bob the builder", "fireman sam",
    "postman pat", "kpop demon hunters", "labubu", "wednesday addams", "stranger things",
]


def find_trademarks(*texts: str) -> list[str]:
    """Return any brand/character names found in the given texts (empty list = clean)."""
    joined = " ".join(t for t in texts if t).lower()
    hits = []
    for name in TRADEMARKS:
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", joined):
            hits.append(name)
    return hits


def banned_listing_words(text: str) -> list[str]:
    words = settings.load_config()["listing"]["banned_words"]
    low = text.lower()
    hits = [w for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low)]
    if re.search(r"\b(19|20)\d\d\b", low):
        hits.append("a year")
    return hits
