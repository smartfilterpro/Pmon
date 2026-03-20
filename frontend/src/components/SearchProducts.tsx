import { useState } from 'react';
import { searchTarget, addProduct, type TargetSearchResult } from '../hooks/useApi';
import { Search, Plus, Loader, ShoppingCart, Check } from 'lucide-react';
import './SearchProducts.css';

interface Props {
  refresh: () => void;
}

export default function SearchProducts({ refresh }: Props) {
  const [keyword, setKeyword] = useState('');
  const [results, setResults] = useState<TargetSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');
  const [added, setAdded] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState<Set<string>>(new Set());
  const [hasSearched, setHasSearched] = useState(false);

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!keyword.trim() || searching) return;

    setSearching(true);
    setError('');
    setResults([]);
    setAdded(new Set());
    setHasSearched(true);
    try {
      const res = await searchTarget(keyword.trim());
      setResults(res);
      if (res.length === 0) setError('No products found');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSearching(false);
    }
  };

  const handleAdd = async (result: TargetSearchResult, autoCheckout: boolean) => {
    if (added.has(result.tcin) || adding.has(result.tcin)) return;
    setAdding(prev => new Set(prev).add(result.tcin));
    try {
      await addProduct(result.url, result.title, 1, autoCheckout);
      setAdded(prev => new Set(prev).add(result.tcin));
      refresh();
    } finally {
      setAdding(prev => {
        const next = new Set(prev);
        next.delete(result.tcin);
        return next;
      });
    }
  };

  const statusLabel = (r: TargetSearchResult) => {
    if (r.availability_status === 'IN_STOCK' || r.is_purchasable) return 'In Stock';
    if (r.availability_status === 'OUT_OF_STOCK') return 'Out of Stock';
    return r.availability_status || 'Unknown';
  };

  const statusClass = (r: TargetSearchResult) => {
    if (r.availability_status === 'IN_STOCK' || r.is_purchasable) return 'in-stock';
    if (r.availability_status === 'OUT_OF_STOCK') return 'out-of-stock';
    return 'unknown';
  };

  return (
    <div className="search-section">
      <form className="search-form" onSubmit={handleSearch}>
        <Search size={16} className="search-icon" />
        <input
          type="text"
          className="search-input"
          placeholder="Search Target products (e.g. &quot;PS5 console&quot;, &quot;Pokemon cards&quot;)"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <button type="submit" className="search-btn" disabled={searching || !keyword.trim()}>
          {searching ? <Loader size={14} className="spinner" /> : <Search size={14} />}
          {searching ? 'Searching...' : 'Search'}
        </button>
      </form>

      {error && <p className="search-error">{error}</p>}

      {results.length > 0 && (
        <div className="search-results">
          {results.map((r) => (
            <div key={r.tcin} className={`search-result-card ${added.has(r.tcin) ? 'added' : ''}`}>
              {r.image_url && (
                <img
                  src={r.image_url}
                  alt=""
                  className="search-result-img"
                  loading="lazy"
                />
              )}
              <div className="search-result-info">
                <p className="search-result-title">{r.title || `TCIN ${r.tcin}`}</p>
                <div className="search-result-meta">
                  <span className="search-result-price">{r.price || 'No price'}</span>
                  <span className={`search-result-status status-${statusClass(r)}`}>
                    {statusLabel(r)}
                  </span>
                  <span className="search-result-tcin">TCIN {r.tcin}</span>
                </div>
              </div>
              <div className="search-result-actions">
                {added.has(r.tcin) ? (
                  <span className="added-badge"><Check size={14} /> Added</span>
                ) : (
                  <>
                    <button
                      className="search-add-btn"
                      onClick={() => handleAdd(r, false)}
                      disabled={adding.has(r.tcin)}
                      title="Add to monitor"
                    >
                      <Plus size={14} />
                      Monitor
                    </button>
                    <button
                      className="search-add-btn auto"
                      onClick={() => handleAdd(r, true)}
                      disabled={adding.has(r.tcin)}
                      title="Add with auto-checkout"
                    >
                      <ShoppingCart size={14} />
                      Auto-buy
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {hasSearched && !searching && results.length === 0 && !error && (
        <p className="search-empty">No results found for "{keyword}"</p>
      )}
    </div>
  );
}
