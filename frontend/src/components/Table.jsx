export function Td({ label, children, className = "", ...rest }) {
  const cls = [className].filter(Boolean).join(" ") || undefined;
  return (
    <td data-label={label} className={cls} {...rest}>
      <div className="td-val">{children}</div>
    </td>
  );
}

export function TdActions({ label = "Actions", children, className = "", ...rest }) {
  const cls = ["td-actions", className].filter(Boolean).join(" ");
  return (
    <td data-label={label} className={cls} {...rest}>
      <div className="td-val">{children}</div>
    </td>
  );
}
