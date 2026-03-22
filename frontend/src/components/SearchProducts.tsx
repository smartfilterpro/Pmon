import { useState } from 'react';
import { searchProducts, addProduct, type SearchResult, type Retailer } from '../hooks/useApi';
import { Search, Plus, Loader, ShoppingCart, Check, Store } from 'lucide-react';
import './SearchProducts.css';

interface Props {
  refresh: () => void;
}

const RETAILER_OPTIONS: { value: Retailer; label: string; color: string }[] = [
  { value: 'target', label: 'Target', color: '#cc0000' },
  { value: 'bestbuy', label: 'Best Buy', color: '#0046be' },
];

export default function SearchProducts({ refresh }: Props) {
  const [keyword, setKeyword] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');
  const [added, setAdded] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState<Set<string>>(new Set());
  const [hasSearched, setHasSearched] = useState(false);
  const [targetOnly, setTargetOnly] = useState(false);
  const [includeOos, setIncludeOos] = useState(false);
  const [selectedRetailers, setSelectedRetailers] = useState<Set<Retailer>>(new Set(['target']));

  const toggleRetailer = (r: Retailer) => {
    setSelectedRetailers(prev => {
      const next = new Set(prev);
      if (next.has(r)) {
        if (next.size > 1) next.delete(r); // keep at least one selected
      } else {
        next.add(r);
      }
      return next;
    });
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!keyword.trim() || searching) return;

    setSearching(true);
    setError('');
    setResults([]);
    setAdded(new Set());
    setHasSearched(true);
    try {
      const retailers = Array.from(selectedRetailers);
      const { results: res, errors: searchErrors } = await searchProducts(keyword.trim(), {
        retailers,
        soldByTargetOnly: targetOnly,
        includeOutOfStock: includeOos,
      });
      setResults(res);
      if (searchErrors.length > 0) {
        setError(searchErrors.join('; '));
      } else if (res.length === 0) {
        setError('No products found');
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSearching(false);
    }
  };

  const resultKey = (r: SearchResult) => `${r.retailer}:${r.tcin}`;

  const handleAdd = async (result: SearchResult, autoCheckout: boolean) => {
    const key = resultKey(result);
    if (added.has(key) || adding.has(key)) return;
    setAdding(prev => new Set(prev).add(key));
    try {
      await addProduct(result.url, result.title, 1, autoCheckout);
      setAdded(prev => new Set(prev).add(key));
      refresh();
    } finally {
      setAdding(prev => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  };

  const statusLabel = (r: SearchResult) => {
    if (r.release_label) return r.release_label;
    if (r.availability_status === 'IN_STOCK' || r.is_purchasable) return 'In Stock';
    if (r.availability_status === 'OUT_OF_STOCK') return 'Out of Stock';
    if (r.availability_status === 'PRE_ORDER') return 'Pre-order';
    if (r.availability_status === 'COMING_SOON') return 'Coming Soon';
    return r.availability_status || 'Unknown';
  };

  const statusClass = (r: SearchResult) => {
    if (r.release_label || r.availability_status === 'PRE_ORDER' || r.availability_status === 'COMING_SOON') return 'upcoming';
    if (r.availability_status === 'IN_STOCK' || r.is_purchasable) return 'in-stock';
    if (r.availability_status === 'OUT_OF_STOCK') return 'out-of-stock';
    return 'unknown';
  };

  const retailerLabel = (r: SearchResult) => {
    const opt = RETAILER_OPTIONS.find(o => o.value === r.retailer);
    return opt?.label || r.retailer;
  };

  const showTargetOnly = selectedRetailers.has('target');

  return (
    <div className="search-section">
      <form className="search-form" onSubmit={handleSearch}>
        <Search size={16} className="search-icon" />
        <input
          type="text"
          className="search-input"
          placeholder={selectedRetailers.size > 1 ? 'Search all retailers (keyword or URL)' : `Search ${Array.from(selectedRetailers).map(r => RETAILER_OPTIONS.find(o => o.value === r)?.label).join(', ')}`}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <div className="retailer-toggles">
          {RETAILER_OPTIONS.map(opt => (
            <button
              key={opt.value}
              type="button"
              className={`retailer-toggle ${selectedRetailers.has(opt.value) ? 'active' : ''}`}
              style={{ '--retailer-color': opt.color } as React.CSSProperties}
              onClick={() => toggleRetailer(opt.value)}
              title={`Search ${opt.label}`}
            >
              {opt.label}
            </button>
          ))}
        </div>
        {showTargetOnly && (
          <label className="filter-label">
            <input
              type="checkbox"
              checked={targetOnly}
              onChange={(e) => setTargetOnly(e.target.checked)}
            />
            <Store size={13} />
            Sold by Target
          </label>
        )}
        <label className="filter-label">
          <input
            type="checkbox"
            checked={includeOos}
            onChange={(e) => setIncludeOos(e.target.checked)}
          />
          Include OOS
        </label>
        <button type="submit" className="search-btn" disabled={searching || !keyword.trim()}>
          {searching ? <Loader size={14} className="spinner" /> : <Search size={14} />}
          {searching ? 'Searching...' : 'Search'}
        </button>
      </form>

      {error && <p className="search-error">{error}</p>}

      {results.length > 0 && (
        <div className="search-results">
          {results.map((r) => {
            const key = resultKey(r);
            return (
              <div key={key} className={`search-result-card ${added.has(key) ? 'added' : ''}`}>
                {r.image_url && (
                  <img
                    src={r.image_url}
                    alt=""
                    className="search-result-img"
                    loading="lazy"
                  />
                )}
                <div className="search-result-info">
                  <p className="search-result-title">{r.title || `ID ${r.tcin}`}</p>
                  <div className="search-result-meta">
                    <span className={`search-result-retailer retailer-${r.retailer}`}>
                      {retailerLabel(r)}
                    </span>
                    <span className="search-result-price">{r.price || 'No price'}</span>
                    <span className={`search-result-status status-${statusClass(r)}`}>
                      {statusLabel(r)}
                    </span>
                    <span className={`search-result-seller ${r.sold_by === 'Target' || r.sold_by === 'Best Buy' ? 'seller-1p' : 'seller-3p'}`}>
                      {r.sold_by || 'Unknown seller'}
                    </span>
                    <span className="search-result-tcin">{r.retailer === 'target' ? `TCIN ${r.tcin}` : `SKU ${r.tcin}`}</span>
                  </div>
                </div>
                <div className="search-result-actions">
                  {added.has(key) ? (
                    <span className="added-badge"><Check size={14} /> Added</span>
                  ) : (
                    <>
                      <button
                        className="search-add-btn"
                        onClick={() => handleAdd(r, false)}
                        disabled={adding.has(key)}
                        title="Add to monitor"
                      >
                        <Plus size={14} />
                        Monitor
                      </button>
                      <button
                        className="search-add-btn auto"
                        onClick={() => handleAdd(r, true)}
                        disabled={adding.has(key)}
                        title="Add with auto-checkout"
                      >
                        <ShoppingCart size={14} />
                        Auto-buy
                      </button>
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {hasSearched && !searching && results.length === 0 && !error && (
        <p className="search-empty">No results found for "{keyword}"</p>
      )}
    </div>
  );
}
