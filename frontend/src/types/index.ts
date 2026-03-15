export interface Product {
  url: string;
  name: string;
  retailer: string;
  quantity: number;
  auto_checkout: boolean;
  status: 'in_stock' | 'out_of_stock' | 'unknown' | 'error';
  price: string;
  timestamp: string;
  error: string;
}

export interface CheckoutEntry {
  url: string;
  product_name: string;
  retailer: string;
  status: string;
  order_number: string;
  error_message: string;
  created_at: string;
}

export interface StatusResponse {
  is_running: boolean;
  started_at: string | null;
  products: Product[];
  checkouts: CheckoutEntry[];
}

export interface User {
  user_id: number;
  username: string;
  totp_enabled: boolean;
}

export interface ErrorEntry {
  id: number;
  level: string;
  source: string;
  message: string;
  details: string;
  created_at: string;
}
