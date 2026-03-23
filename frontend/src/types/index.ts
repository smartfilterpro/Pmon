export interface Product {
  url: string;
  name: string;
  retailer: string;
  quantity: number;
  auto_checkout: boolean;
  status: 'in_stock' | 'out_of_stock' | 'unknown' | 'error';
  price: string;
  image_url: string;
  timestamp: string;
  error: string;
  last_in_stock: string;
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

export interface OtpRequest {
  id: number;
  retailer: string;
  context: string;
  status: string;
  created_at: string;
}

export interface StatusResponse {
  is_running: boolean;
  started_at: string | null;
  products: Product[];
  checkouts: CheckoutEntry[];
  pending_otp: OtpRequest | null;
  total_spent: number;
  spend_limit: number;
}

export interface User {
  user_id: number;
  username: string;
  is_admin: boolean;
  totp_enabled: boolean;
}

export interface ManagedUser {
  id: number;
  username: string;
  is_admin: number;
  approved: number;
  created_at: string;
  last_login: string | null;
}

export interface ErrorEntry {
  id: number;
  level: string;
  source: string;
  message: string;
  details: string;
  created_at: string;
}
