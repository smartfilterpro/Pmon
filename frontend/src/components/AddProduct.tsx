import { useState } from 'react';
import { addProduct } from '../hooks/useApi';
import { Plus } from 'lucide-react';
import './AddProduct.css';

interface Props {
  refresh: () => void;
}

export default function AddProduct({ refresh }: Props) {
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [quantity, setQuantity] = useState(1);
  const [maxPrice, setMaxPrice] = useState(0);
  const [auto, setAuto] = useState(false);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;

    setLoading(true);
    try {
      await addProduct(url.trim(), name.trim(), quantity, auto, maxPrice);
      setUrl('');
      setName('');
      setQuantity(1);
      setMaxPrice(0);
      setAuto(false);
      refresh();
    } finally {
      setLoading(false);
    }
  };

  return (
    <form className="add-form" onSubmit={handleSubmit}>
      <input
        type="url"
        className="add-url"
        placeholder="Product URL (Target, Walmart, Best Buy, Pokemon Center, Amazon)"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        required
      />
      <input
        type="text"
        className="add-name"
        placeholder="Name (optional)"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <input
        type="number"
        className="add-qty"
        min={1}
        max={10}
        value={quantity}
        onChange={(e) => setQuantity(Number(e.target.value))}
        title="Quantity"
      />
      <input
        type="number"
        className="add-max-price"
        min={0}
        step={0.01}
        value={maxPrice || ''}
        onChange={(e) => setMaxPrice(Number(e.target.value) || 0)}
        placeholder="Max $"
        title="Maximum price — skips auto-buy if price exceeds this (0 = no limit)"
      />
      <label className="auto-label">
        <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
        Auto-buy
      </label>
      <button type="submit" className="add-btn" disabled={loading || !url.trim()}>
        <Plus size={16} />
        Add
      </button>
    </form>
  );
}
