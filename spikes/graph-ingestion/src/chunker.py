#!/usr/bin/env python3
"""HTML chunking component.

Chunks HTML by article boundaries to preserve regulatory structure.
Supports EU EUR-Lex format (class="oj-ti-art") as primary path, standard <article> tags as alternative.

The chunker processes official EU regulation HTML from EUR-Lex which uses paragraph headers
instead of <article> tags. It extracts content between consecutive <p class="oj-ti-art"> headers,
properly preserving regulatory structure (e.g., 71 articles → 71 chunks).
"""

from bs4 import BeautifulSoup


def _extract_article_chunks(soup: BeautifulSoup) -> list[dict]:
    """Extract chunks from article headers.
    
    Priority order:
    1. EU EUR-Lex structure: <p class="oj-ti-art">Article X</p> (primary for official EU docs)
    2. Standard HTML5 <article> tags (fallback for other sources)
    
    Returns empty list if neither structure found - caller must handle error.
    """
    # Try 1: EU EUR-Lex format (primary path for official EU regulations)
    article_headers = soup.find_all('p', class_='oj-ti-art')
    
    if article_headers:
        chunks = []
        for i, header in enumerate(article_headers):
            article_id = f"art_{header.get_text(strip=True)}"
            
            # Collect all paragraphs until the next article
            content_parts = []
            current = header.find_next('p')
            
            while current:
                if current.name == 'p' and 'oj-ti-art' in current.get('class', []):
                    break
                
                text = current.get_text(strip=True)
                if text:
                    content_parts.append(text)
                
                current = current.find_next('p')
            
            if content_parts:
                chunks.append({
                    'article_id': article_id,
                    'content': '\n'.join(content_parts)
                })
        
        return chunks
    
    # Try 2: Standard HTML5 <article> tags as alternative
    articles = soup.find_all('article')
    
    if not articles:
        return []
    
    chunks = []
    for i, article in enumerate(articles):
        chunk = {
            'article_id': article.get('id', f'art_{i}'),
            'content': article.get_text(strip=True, separator=' ')
        }
        chunks.append(chunk)
    
    return chunks


def chunk_by_article(html: str) -> list[dict]:
    """Chunk HTML by article boundaries.
    
    Priority order:
    1. EU EUR-Lex format: <p class="oj-ti-art">Article X</p> headers (primary)
    2. Standard HTML5 <article> tags (fallback for other sources)
    
    Args:
        html: HTML content string
        
    Returns:
        List of chunks, each with 'article_id' and 'content'
        
    Raises:
        ValueError if no article structure found (no fallback to paragraph batching)
    """
    if not html or not html.strip():
        return []
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # Try article-level chunking
    chunks = _extract_article_chunks(soup)
    
    # If no articles found, raise error - regulatory docs MUST have structure
    if not chunks:
        raise ValueError(
            "No article structure found in HTML. Expected either:\n"
            "- <p class=\"oj-ti-art\"> headers (EU EUR-Lex format)\n"
            "- <article> tags (standard HTML5)"
        )
    
    return chunks
