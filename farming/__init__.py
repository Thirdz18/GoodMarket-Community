from .routes import farming_bp


def init_farming(app):
    """Initialize GoodMarket Chicken Farming module."""
    try:
        app.register_blueprint(farming_bp)
        import logging
        logging.getLogger(__name__).info("✅ Chicken Farming module initialized")
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"❌ Chicken Farming initialization failed: {e}")
        return False


__all__ = ["farming_bp", "init_farming"]
