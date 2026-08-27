import { Suspense } from "react";
import { ResetPasswordForm } from "./reset-password-form";

// The backend's password-reset email links to this exact route
// (see FRONTEND_URL + "/reset-password?uid=...&token=..." in
// apps/users/views.py PasswordResetRequestView).
export default function ResetPasswordPage() {
  return (
    <Suspense fallback={null}>
      <ResetPasswordForm />
    </Suspense>
  );
}
