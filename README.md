# phpIPAM a NetBox

Este repositorio contiene un script en Python pensado para extraer la información de subredes e IPs desde un servidor phpIPAM y generar ficheros CSV compatibles con el importador de NetBox (probado con NetBox v4.4). El flujo previsto es:

1. Obtener todos los clientes ("Customers"), subredes e IPs disponibles mediante la API REST de phpIPAM.
2. Convertir los datos a un formato comprensible para NetBox, respetando el estatus de cada elemento y los clientes asociados.
3. Guardar los resultados en una carpeta (`output/` por defecto) con los ficheros `tenants.csv`, `prefixes.csv` e `ip-addresses.csv` listos para importar en NetBox.

## Requisitos

* Python 3.9 o superior (el contenedor utiliza Python 3.11).
* Acceso HTTP(s) hacia el servidor phpIPAM (se utiliza únicamente la API REST).
* No se necesita instalar dependencias externas: el cliente HTTP usa únicamente librerías estándar.

## Configuración previa en phpIPAM

1. Activar la API en **Administration → API** y crear una aplicación con su `App ID`.
2. Autorizar a un usuario para utilizar la API y asignarle los permisos necesarios sobre los objetos que se quieran exportar.
3. (Opcional) Si se utiliza el módulo de *Customers*, asegurarse de que los clientes están correctamente asociados a las subredes/IPs.

## Uso básico

```bash
python -m phpipam_to_netbox.cli \
  --phpipam-url https://phpipam.ejemplo.com \
  --app-id MiAplicacion \
  --username usuario_api \
  --password 'MiContraseñaSegura'
```

La ejecución anterior escribirá los ficheros CSV dentro de la carpeta `output/`. Si se quiere elegir otra ruta basta con añadir `--output-dir ./mis-datos`.

### Parámetros útiles

* `--token`: Permite usar un token de la API en lugar de usuario/contraseña.
* `--customer NombreCliente`: Exporta únicamente los objetos asociados al customer indicado (se puede repetir el parámetro para varios clientes). Acepta tanto el nombre como el ID numérico.
* `--skip-ip-addresses`: Genera únicamente los ficheros de tenants y prefijos.
* `--skip-prefixes`: Exporta solo las direcciones IP.
* `--prefix-status` y `--ip-status`: Definen el estado por defecto asignado en NetBox.
* `--no-map-tags-to-status`: Deshabilita la traducción automática entre los *tags* de phpIPAM y los estados de NetBox para las direcciones IP.
* `--tag-status-map`: Permite personalizar la traducción de *tags* en formato `id=status`. También acepta nombres, por ejemplo `Reserved=reserved`.
* `--tenant-group`: Asigna el mismo Tenant Group a todos los tenants generados en NetBox.

### Resultado

El comando genera hasta tres ficheros CSV:

* `tenants.csv`: listado de clientes detectados (solo si se ha habilitado `--map-customers-to-tenants`).
* `prefixes.csv`: todas las subredes exportadas.
* `ip-addresses.csv`: las direcciones IP encontradas en cada subred.

Los campos `tenant` y `tenant__slug` se rellenan automáticamente para facilitar la importación en NetBox. Durante la importación en NetBox se pueden eliminar las columnas no deseadas.

## Buenas prácticas

* Trabajar inicialmente con un entorno de pruebas de NetBox para validar el resultado antes de aplicarlo en producción.
* Revisar los ficheros CSV y adaptar columnas adicionales según las necesidades (por ejemplo, `site`, `role`, etc.).
* Guardar los CSV generados bajo control de versiones para tener un histórico de cambios.

## Tests

El repositorio incluye pruebas unitarias básicas ejecutables con:

```bash
python -m unittest
```

## Licencia

[MIT](LICENSE)
