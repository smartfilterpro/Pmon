export interface Product {
  url: string;
  name: string;
  retailer: string;
  status: 'in_stock' | 'out_of_stock' | 'unknown' | 'error';
  price: string;
  timestamp: string;
  error: string;
  auto_checkout: boolean;
}

export interface CheckoutEntry {
  url: string;
  name: string;
  retailer: string;
  status: 'idle' | 'attempting' | 'success' | 'failed';
  order_number: string;
  error: string;
  timestamp: string;
}

export interface StatusResponse {
  is_running: boolean;
  started_at: string | null;
  products: Product[];
  checkouts: CheckoutEntry[];
}

export interface Settings {
  poll_interval: number;
  discord_webhook: string;
}
