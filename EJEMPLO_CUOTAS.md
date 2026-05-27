# Ejemplo: Agregar Rastreo de Productos con Cuotas/Kambios

Este archivo muestra cómo puedes agregar productos que quieras comprar en cuotas.

## Estructur Propuesta para el config.json

```json
{
  "products": [
    {
      "id": "iphone_16",
      "kind": "product",
      "name": "📱 iPhone 16 Pro - Falabella",
      "url": "https://www.falabella.com.co/falabella-co/product/...",
      "target_price": 5500000,
      "currency": "COP",
      "active": true,
      "notes": "Precio de contado. Ver opciones de cuotas en Falabella.",
      "installments": {
        "enabled": true,
        "available_plans": [3, 6, 12],
        "preferred_plan": 12,
        "note": "Consultar cuota mensual sin interés"
      }
    },
    {
      "id": "macbook_m4",
      "kind": "product", 
      "name": "💻 MacBook Air M4 - Alkosto",
      "url": "https://www.alkosto.com/...",
      "target_price": 8500000,
      "currency": "COP",
      "active": true,
      "installments": {
        "enabled": true,
        "available_plans": [6, 12, 18, 24],
        "preferred_plan": 18,
        "interest_rate": 0,
        "note": "Financiación sin interés"
      }
    },
    {
      "id": "ps5_pro",
      "kind": "product",
      "name": "🎮 PS5 Pro - MercadoLibre",
      "url": "https://www.mercadolibre.com.co/...",
      "target_price": 4500000,
      "currency": "COP",
      "active": true,
      "installments": {
        "enabled": true,
        "available_plans": [3, 6, 12],
        "preferred_plan": 12,
        "credit_cards": ["Visa", "Mastercard", "American Express"],
        "note": "Verificar meses sin interés por banco"
      }
    }
  ]
}
```

## Cómo se Mostraría en el Dashboard

Para cada producto con cuotas, se vería:

```
📱 iPhone 16 Pro - Falabella
Estado: ✅ OK | Fuente: Falabella
Precio: $ 5,500,000 COP

💰 Opciones de Cuotas:
  • 3 cuotas: $1,833,333 c/u
  • 6 cuotas: $916,667 c/u
  • 12 cuotas: $458,333 c/u ← RECOMENDADO

📈 Histórico de precios: [gráfico sparkline]

[Guardar] [Pausar] [Abrir en Falabella]
```

## Integración en tracker.py

Para rastrear cuotas, necesitarías:

```python
def calculate_installments(price: float, plans: list[int]) -> dict[int, float]:
    """Calcula cuota mensual para cada plan."""
    return {
        plan: price / plan
        for plan in plans
    }

def format_installment_alert(product: dict, price: float) -> str:
    """Genera alerta con opciones de cuotas."""
    inst = product.get("installments", {})
    if not inst.get("enabled"):
        return f"{product['name']}: ${price:,.0f}"
    
    plans = calculate_installments(price, inst.get("available_plans", []))
    preferred = inst.get("preferred_plan", 12)
    preferred_fee = plans.get(preferred, price)
    
    msg = f"{product['name']}\n"
    msg += f"💵 Precio: ${price:,.0f}\n"
    msg += f"📅 Mejores cuotas:\n"
    for plan, fee in sorted(plans.items()):
        mark = " ← ⭐" if plan == preferred else ""
        msg += f"   {plan} meses: ${fee:,.0f}/mes{mark}\n"
    
    return msg
```

## Qué Necesitas Hacer

1. **Agregar productos con cuotas al config.json**
   - Usa URLs correctas de las tiendas
   - Define planes de pago disponibles
   - Indica plan preferido

2. **Opcionalmente: Mejorar tracker.py**
   - Integrar cálculo de cuotas
   - Mostrar alertas con opciones de cuotas

3. **Mejoras en web_app.py**
   - Tabla de cuotas por producto
   - Comparador de opciones de pago

## Tiendas que Soportan Cuotas

✅ **Falabella** - Hasta 12 cuotas sin interés
✅ **Alkosto** - Hasta 24 cuotas  
✅ **MercadoLibre** - Depende del vendedor (3-24 cuotas)
✅ **Linio** - 3, 6, 12 cuotas
✅ **Ktronix** - Según producto
✅ **Amazon** - Crédito de Amazon para cuotas

## ¿Quieres que Implemente?

Si quieres que integre:
1. ☐ Sistema de cálculo de cuotas
2. ☐ Alertas con opciones de pago
3. ☐ Tabla comparativa en dashboard
4. ☐ Guarda de mejor cuota encontrada

¡Avísame!
