# E2 schema evidence (sanitized fragment)

Captured 2026-10-30 at about 12:35 ET by Claude (corrected from "12:37" in commit b292a2e, which was later than the clock reading). Source: the docs portal JSON entry behind https://docs.athenahealth.com/api/api-ref/appointment-confirmation-status, URL https://docs.athenahealth.com/v1/api/entries/exploreDocs?urlAlias=%2Fapi-ref%2Fappointment-confirmation-status&include=5. It was fetched by in-page JavaScript in the Claude desktop browser pane, logged out.

The portal JSON is the only source for these element fields; the rendered page shows `status` only as an array. Below is the fragment as extracted: for each endpoint object, its method, endpointUrl, operationId, apiCatalog, the 200 `application/json` schema and the request body. Nothing else from the portal payload is reproduced.

What this does **not** state, and E2 does not claim:
- that the vocabulary `statusid` equals the write field `appointmentconfirmationid`;
- any `required` list for vocabulary elements or confirmation-read fields;
- any semantics beyond the descriptions.

```json
[
  {
    "method": "GET",
    "endpointUrl": "/v1/{practiceid}/appointments/{appointmentid}/confirmationstatus",
    "operationId": "getPracticeidAppointmentsAppointmentidConfirmationstatus",
    "apiCatalog": "Appointments.yaml",
    "output200schema": {
      "properties": {
        "confirmationname": {
          "description": "Description of appointment confirmation status.",
          "type": "string"
        },
        "shortname": {
          "description": "If defined then the status is a positive confirmation.",
          "type": "string"
        }
      },
      "type": "object"
    },
    "requestBody": null
  },
  {
    "method": "PUT",
    "endpointUrl": "/v1/{practiceid}/appointments/{appointmentid}/confirmationstatus",
    "operationId": "putPracticeidAppointmentsAppointmentidConfirmationstatus",
    "apiCatalog": "Appointments.yaml",
    "output200schema": {
      "type": "object",
      "properties": {
        "success": {
          "type": "boolean",
          "description": "Whether the operation was successful."
        }
      }
    },
    "requestBody": {
      "application/x-www-form-urlencoded": {
        "schema": {
          "type": "object",
          "required": [
            "appointmentconfirmationid"
          ],
          "properties": {
            "appointmentconfirmationid": {
              "type": "integer",
              "description": "The appointment's confirmation status."
            }
          }
        }
      }
    }
  },
  {
    "method": "GET",
    "endpointUrl": "/v1/{practiceid}/reference/appointmentconfirmationstatus",
    "operationId": "getPracticeidReferenceAppointmentconfirmationstatus",
    "apiCatalog": "Appointments.yaml",
    "output200schema": {
      "properties": {
        "next": {
          "description": "URL for the next page of results, if any.",
          "type": "string"
        },
        "previous": {
          "description": "URL for the previous page of results, if any.",
          "type": "string"
        },
        "status": {
          "items": {
            "properties": {
              "name": {
                "description": "Appointment Confirmation Status.",
                "type": "string"
              },
              "statusid": {
                "description": "Status Id",
                "type": "integer"
              }
            },
            "type": "object"
          },
          "type": "array"
        },
        "totalcount": {
          "description": "Total number of records matching the request.",
          "type": "integer"
        }
      },
      "required": [
        "totalcount",
        "status"
      ],
      "type": "object"
    },
    "requestBody": null
  }
]
```
