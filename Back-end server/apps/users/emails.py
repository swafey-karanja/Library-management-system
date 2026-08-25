import resend
from django.conf import settings

# Initialise the Resend client with the API key from settings.
# This runs once when the module is first imported.
resend.api_key = settings.RESEND_API_KEY


def send_password_reset_email(user_name: str, to_email: str, reset_url: str) -> bool:
    """
    Sends a password reset email via Resend.

    Args:
        user_name:  The user's display name — used to personalise the email.
        to_email:   The recipient's email address.
        reset_url:  The full frontend URL containing the uid and token.
                    e.g. http://localhost:3000/reset-password?uid=...&token=...

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    try:
        resend.Emails.send(
            {
                "from": settings.RESEND_FROM_EMAIL,
                "to": [to_email],
                "subject": "Reset your Library System password",
                "html": _build_reset_email_html(user_name, reset_url),
            }
        )
        return True
    except Exception as e:
        # Log the error but don't raise — the view handles the response.
        # In production, plug this into a proper logger.
        print(f"[Email error] Failed to send reset email to {to_email}: {e}")
        return False


def _build_reset_email_html(user_name: str, reset_url: str) -> str:
    """Builds the HTML body for the password reset email."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:40px 0;">
        <tr>
          <td align="center">
            <table width="560" cellpadding="0" cellspacing="0"
                   style="background:#ffffff;border-radius:8px;overflow:hidden;
                          box-shadow:0 1px 4px rgba(0,0,0,0.08);">

              <!-- Header -->
              <tr>
                <td style="background-color:#1E3A5F;padding:32px 40px;">
                  <p style="margin:0;font-size:20px;font-weight:bold;color:#ffffff;
                             letter-spacing:0.5px;">
                    Library Management System
                  </p>
                </td>
              </tr>

              <!-- Body -->
              <tr>
                <td style="padding:40px;">
                  <p style="margin:0 0 16px;font-size:16px;color:#1a1a1a;">
                    Hi {user_name},
                  </p>
                  <p style="margin:0 0 24px;font-size:15px;color:#444444;line-height:1.6;">
                    We received a request to reset your password. Click the button
                    below to choose a new one. This link expires in
                    <strong>1 hour</strong>.
                  </p>

                  <!-- CTA button -->
                  <table cellpadding="0" cellspacing="0" style="margin:0 0 32px;">
                    <tr>
                      <td style="background-color:#2E75B6;border-radius:6px;">
                        <a href="{reset_url}"
                           style="display:inline-block;padding:14px 32px;
                                  font-size:15px;font-weight:bold;
                                  color:#ffffff;text-decoration:none;">
                          Reset my password
                        </a>
                      </td>
                    </tr>
                  </table>

                  <p style="margin:0 0 8px;font-size:13px;color:#888888;">
                    If the button doesn't work, copy and paste this link into your browser:
                  </p>
                  <p style="margin:0 0 32px;font-size:12px;color:#2E75B6;
                             word-break:break-all;">
                    {reset_url}
                  </p>

                  <hr style="border:none;border-top:1px solid #eeeeee;margin:0 0 24px;">

                  <p style="margin:0;font-size:13px;color:#aaaaaa;line-height:1.6;">
                    If you didn't request a password reset, you can safely ignore
                    this email — your password will not change.
                  </p>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="background-color:#f9f9f9;padding:20px 40px;
                            border-top:1px solid #eeeeee;">
                  <p style="margin:0;font-size:12px;color:#aaaaaa;text-align:center;">
                    This email was sent by the Library Management System.
                    Please do not reply to this email.
                  </p>
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """


def send_activation_email(user_name: str, to_email: str, activation_url: str) -> bool:
    """
    Sends a welcome + email activation email to a newly created user.

    Args:
        user_name:       The user's display name.
        to_email:        The recipient's email address.
        activation_url:  Full frontend URL with uid + token query params.
                         e.g. http://localhost:3000/activate?uid=...&token=...
    """
    try:
        resend.Emails.send(
            {
                "from": settings.RESEND_FROM_EMAIL,
                "to": [to_email],
                "subject": "Activate your Library System account",
                "html": _build_activation_email_html(user_name, activation_url),
            }
        )
        return True
    except Exception as e:
        print(f"[Email error] Failed to send activation email to {to_email}: {e}")
        return False


def _build_activation_email_html(user_name: str, activation_url: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:40px 0;">
        <tr>
          <td align="center">
            <table width="560" cellpadding="0" cellspacing="0"
                   style="background:#ffffff;border-radius:8px;overflow:hidden;
                          box-shadow:0 1px 4px rgba(0,0,0,0.08);">

              <!-- Header -->
              <tr>
                <td style="background-color:#1E3A5F;padding:32px 40px;">
                  <p style="margin:0;font-size:20px;font-weight:bold;color:#ffffff;">
                    Welcome to the Library Management System
                  </p>
                </td>
              </tr>

              <!-- Body -->
              <tr>
                <td style="padding:40px;">
                  <p style="margin:0 0 16px;font-size:16px;color:#1a1a1a;">
                    Hi {user_name},
                  </p>
                  <p style="margin:0 0 24px;font-size:15px;color:#444444;line-height:1.6;">
                    Your account has been created. To get started, please confirm
                    your email address by clicking the button below. This link
                    expires in <strong>24 hours</strong>.
                  </p>

                  <!-- CTA button -->
                  <table cellpadding="0" cellspacing="0" style="margin:0 0 32px;">
                    <tr>
                      <td style="background-color:#2E75B6;border-radius:6px;">
                        <a href="{activation_url}"
                           style="display:inline-block;padding:14px 32px;
                                  font-size:15px;font-weight:bold;
                                  color:#ffffff;text-decoration:none;">
                          Confirm my email address
                        </a>
                      </td>
                    </tr>
                  </table>

                  <p style="margin:0 0 8px;font-size:13px;color:#888888;">
                    If the button doesn't work, copy and paste this link:
                  </p>
                  <p style="margin:0 0 32px;font-size:12px;color:#2E75B6;word-break:break-all;">
                    {activation_url}
                  </p>

                  <hr style="border:none;border-top:1px solid #eeeeee;margin:0 0 24px;">

                  <p style="margin:0;font-size:13px;color:#aaaaaa;line-height:1.6;">
                    If you were not expecting this email, you can safely ignore it.
                    Your account will remain inactive until the link is clicked.
                  </p>
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td style="background-color:#f9f9f9;padding:20px 40px;border-top:1px solid #eeeeee;">
                  <p style="margin:0;font-size:12px;color:#aaaaaa;text-align:center;">
                    This email was sent by the Library Management System.
                    Please do not reply to this email.
                  </p>
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """
