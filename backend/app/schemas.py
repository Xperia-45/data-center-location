from pydantic import BaseModel, Field, field_validator
class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    lat: float; lon: float; radius_km: float = Field(default=5, gt=0, le=50)
    footprint_ha: float = Field(default=10, gt=0, le=1000)
    water_mld: float = Field(default=1.0, ge=0, le=100)
    power_mw: float = Field(default=20, gt=0, le=2000)
    cooling: str = "air"
    built_up_m2: float = Field(default=15000, ge=0, le=2000000)
    @field_validator("lat")
    @classmethod
    def lat_ok(cls,v):
        if not -90<=v<=90: raise ValueError("lat out of range")
        return v
    @field_validator("lon")
    @classmethod
    def lon_ok(cls,v):
        if not -180<=v<=180: raise ValueError("lon out of range")
        return v
class ScenarioIn(BaseModel):
    preset: str = "baseline"
    footprint_ha: float = 10; water_mld: float = 1.0; power_mw: float = 20
    cooling: str = "air"; mitigation: list[str] = []
class LegalSearch(BaseModel):
    query: str = Field(min_length=1, max_length=200)

class SiteRef(BaseModel):
    """Lightweight site reference used to place an optional parcel geometry in context."""
    lat: float; lon: float; radius_km: float = Field(default=5, gt=0, le=50)
    @field_validator("lat")
    @classmethod
    def ref_lat_ok(cls, v):
        if not -90 <= v <= 90: raise ValueError("lat out of range")
        return v
    @field_validator("lon")
    @classmethod
    def ref_lon_ok(cls, v):
        if not -180 <= v <= 180: raise ValueError("lon out of range")
        return v

class ParcelValidateIn(BaseModel):
    """
    Optional user parcel geometry (GeoJSON Polygon or MultiPolygon).
    Validated with Shapely; rejected with 422 when unusable and 503 when Shapely is absent.
    """
    geojson: dict
    site: SiteRef | None = None


class LandscapeProject(BaseModel):
    """Optional project inputs used by the development impact layer."""
    footprint_ha: float = Field(default=10, gt=0, le=1000)
    water_mld: float = Field(default=1.0, ge=0, le=100)
    power_mw: float = Field(default=20, gt=0, le=2000)

class LandscapeRequest(BaseModel):
    """
    Landscape request for free coordinates, with an optional parcel polygon.
    The base layer stays scenario-independent; only the impact layer follows `preset`.
    """
    lat: float; lon: float
    radius_km: float = Field(default=5, gt=0, le=50)
    grid_size: int = Field(default=20, ge=2, le=32)
    layers: list[str] | None = None
    osm_layers: list[str] | None = None
    include_geometry: bool = False
    preset: str = "baseline"
    parcel_geojson: dict | None = None
    project: LandscapeProject | None = None
    refresh: bool = False
    @field_validator("lat")
    @classmethod
    def landscape_lat_ok(cls, v):
        if not -90 <= v <= 90: raise ValueError("lat out of range")
        return v
    @field_validator("lon")
    @classmethod
    def landscape_lon_ok(cls, v):
        if not -180 <= v <= 180: raise ValueError("lon out of range")
        return v
