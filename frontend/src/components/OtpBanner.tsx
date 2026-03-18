import { useState } from 'react';
import { submitOtp } from '../hooks/useApi';
import type { OtpRequest } from '../types';

interface OtpBannerProps {
  otp: OtpRequest;
  onSubmitted: () => void;
}

export default function OtpBanner({ otp, onSubmitted }: OtpBannerProps) {
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = code.trim().replace(/[\s-]/g, '');
    if (!trimmed) return;

    setSubmitting(true);
    setError('');
    try {
      const result = await submitOtp(otp.id, trimmed);
      if (result.ok) {
        setSuccess(true);
        setTimeout(() => onSubmitted(), 2000);
      } else {
        setError(result.error || 'Failed to submit code');
      }
    } catch (err) {
      setError('Network error');
    } finally {
      setSubmitting(false);
    }
  };

  if (success) {
    return (
      <div className="otp-banner otp-success">
        Code submitted! Entering it now...
      </div>
    );
  }

  return (
    <div className="otp-banner">
      <div className="otp-banner-text">
        <strong>{otp.retailer.toUpperCase()}</strong> needs a verification code.
        Check your texts and enter the code below.
      </div>
      <form className="otp-form" onSubmit={handleSubmit}>
        <input
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="Enter code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          maxLength={10}
          className="otp-input"
          autoFocus
        />
        <button type="submit" disabled={submitting || !code.trim()} className="otp-submit">
          {submitting ? 'Sending...' : 'Submit'}
        </button>
      </form>
      {error && <div className="otp-error">{error}</div>}
    </div>
  );
}
